"""Improvement Proposals: deterministic learning from repeated Run Evidence.

A proposal is a *record* until it clears the Adoption Gate. This module
covers the full lifecycle:

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
3. **Adoption Gate** — :func:`approve_adoption` records an attributed,
   content-hashed human approval (``adoption.json``) of a passing proposal.
   Like an Approved Revision, the approval binds to the exact proposal
   content: an edit after approval makes the approval stale.
4. **Application** — :func:`apply_proposal_to_baseline` is the single choke
   point through which a proposal may change a baseline. It refuses
   unevaluated, failing, unapproved, stale-approved, and already-applied
   proposals; otherwise it applies the change deterministically and records
   attributed ``applied.json`` evidence.

The upstream-skill counterpart of the same gate also lives here:
:func:`compare_skill_baseline` records a per-file diff of the local skill
baseline against an operator-supplied upstream copy, and
:func:`adopt_skill_file` selectively adopts one reviewed file through an
attributed, content-hashed approval bound to that comparison. Together with
:func:`apply_proposal_to_baseline` these are the only functions in Agentflow
that write a baseline; both sit behind the Adoption Gate, and nothing is ever
auto-adopted.
"""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Callable

from .repository_profile import PROFILE_RELATIVE_PATH

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

# The workflow-configuration baseline lives in Agentflow Home; the Repository
# Profile baseline lives in the Target Repository at PROFILE_RELATIVE_PATH.
WORKFLOW_CONFIG_FILENAME = "workflow-config.json"

# The local skill baseline an upstream comparison targets, relative to the
# Agentflow repository root.
SKILL_BASELINE_RELATIVE_DIR = Path("skills/agentflow")


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


def proposal_content_hash(record: dict) -> str:
    """Stable content hash of a stored proposal record.

    An adoption approval binds to this hash, so any later edit to the
    proposal — whitespace aside — is detectable and makes the approval stale,
    mirroring how an Approved Revision is invalidated by any code change.
    """
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stored_proposal_record(data_dir: Path, proposal_id: str) -> dict:
    proposal_path = _proposal_dir(data_dir, proposal_id) / "proposal.json"
    if not proposal_path.is_file():
        raise ValueError(f"no proposal {proposal_id}")
    return json.loads(proposal_path.read_text(encoding="utf-8"))


def read_proposal(*, data_dir: Path, proposal_id: str) -> dict:
    """Return the proposal record with its derived state and gate evidence.

    State is derived from evidence on disk, mirroring Run State: ``proposed``
    until an evaluation record exists, ``evaluated`` once one does,
    ``approved`` once an adoption approval binds to the current proposal
    content (a stale approval leaves the proposal ``evaluated``), and
    ``applied`` once application evidence exists.
    """
    proposal_dir = _proposal_dir(data_dir, proposal_id)
    record = _stored_proposal_record(data_dir, proposal_id)
    content_hash = proposal_content_hash(record)
    record["state"] = "proposed"
    evaluation_path = proposal_dir / "evaluation.json"
    if evaluation_path.is_file():
        record["evaluation"] = json.loads(evaluation_path.read_text(encoding="utf-8"))
        record["state"] = "evaluated"
    adoption_path = proposal_dir / "adoption.json"
    if adoption_path.is_file():
        adoption = json.loads(adoption_path.read_text(encoding="utf-8"))
        record["adoption"] = adoption
        if (
            record["state"] == "evaluated"
            and adoption["proposal_hash"] == content_hash
        ):
            record["state"] = "approved"
    applied_path = proposal_dir / "applied.json"
    if applied_path.is_file():
        record["applied"] = json.loads(applied_path.read_text(encoding="utf-8"))
        record["state"] = "applied"
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


def approve_adoption(
    *,
    data_dir: Path,
    proposal_id: str,
    approved_by: str,
    now: datetime | None = None,
) -> dict:
    """Record the Adoption Gate's human approval of a passing proposal.

    The approval is attributed and content-hashed: it binds to the exact
    proposal content on disk at approval time, so a proposal edited afterwards
    is stale and cannot be applied until a human re-approves the new content.
    Only a proposal whose evaluation passed may be approved; approval records
    intent and never changes a baseline itself.
    """
    if not approved_by:
        raise ValueError("an adoption approval requires --approved-by")
    proposal = read_proposal(data_dir=data_dir, proposal_id=proposal_id)
    evaluation = proposal.get("evaluation")
    if evaluation is None or not evaluation["passed"]:
        raise ValueError(
            f"proposal {proposal_id} has not passed evaluation; "
            "the Adoption Gate only accepts passing proposals"
        )
    if now is None:
        now = datetime.now(timezone.utc)
    record = {
        "approved_at": now.isoformat(),
        "approved_by": approved_by,
        "proposal_hash": proposal_content_hash(
            _stored_proposal_record(data_dir, proposal_id)
        ),
        "proposal_id": proposal_id,
        "type": "adoption_approved",
    }
    (_proposal_dir(data_dir, proposal_id) / "adoption.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return record


def _advisory_entry(proposal: dict) -> dict:
    """The minimal, deterministic baseline change a proposal applies."""
    return {
        "change": proposal["change"],
        "kind": proposal["kind"],
        "proposal_id": proposal["proposal_id"],
        "subject": proposal["subject"],
    }


def _apply_advisory(baseline_path: Path, entry: dict) -> None:
    """Add an adopted advisory to a JSON baseline document, deterministically.

    Advisories are keyed by proposal id and kept sorted, so applying the same
    adopted proposal to the same baseline always yields the same bytes.
    """
    baseline = (
        json.loads(baseline_path.read_text(encoding="utf-8"))
        if baseline_path.is_file()
        else {}
    )
    advisories = [
        advisory
        for advisory in baseline.get("advisories", [])
        if advisory["proposal_id"] != entry["proposal_id"]
    ]
    advisories.append(entry)
    baseline["advisories"] = sorted(
        advisories, key=lambda advisory: advisory["proposal_id"]
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(baseline, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def apply_proposal_to_baseline(
    *,
    data_dir: Path,
    proposal_id: str,
    repository: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Apply an adopted proposal to its baseline — the single choke point.

    No baseline changes through any other proposal code path. A proposal must
    have passed evaluation, carry an adoption approval whose content hash
    still matches the proposal on disk (an edit after approval is stale), and
    not already be applied. Application is minimal and deterministic: the
    proposal's change is recorded as an adopted advisory in the target
    baseline document — ``workflow_config`` proposals in Agentflow Home's
    ``workflow-config.json``, ``repository_profile`` proposals in the Target
    Repository's profile — and attributed ``applied.json`` evidence is
    written under the proposal directory.
    """
    proposal = read_proposal(data_dir=data_dir, proposal_id=proposal_id)
    evaluation = proposal.get("evaluation")
    if evaluation is None or not evaluation["passed"]:
        raise ValueError(
            f"proposal {proposal_id} has not passed evaluation; "
            "baselines remain unchanged"
        )
    adoption = proposal.get("adoption")
    if adoption is None:
        raise ValueError(
            f"proposal {proposal_id} passed evaluation but has not been "
            "approved through the Adoption Gate; adopt it with "
            f"`agentflow adopt {proposal_id} --approved-by <name>` — a "
            "passing evaluation alone never changes a baseline"
        )
    content_hash = proposal_content_hash(
        _stored_proposal_record(data_dir, proposal_id)
    )
    if adoption["proposal_hash"] != content_hash:
        raise ValueError(
            f"proposal {proposal_id} changed after its adoption approval "
            f"(approved {adoption['proposal_hash'][:12]}, current "
            f"{content_hash[:12]}); the approval is stale — re-approve the "
            "current content before it can change a baseline"
        )
    if proposal.get("applied") is not None:
        raise ValueError(
            f"proposal {proposal_id} was already applied; baselines change "
            "at most once per adoption"
        )
    target = proposal["target"]
    if target == "workflow_config":
        baseline_path = data_dir / WORKFLOW_CONFIG_FILENAME
    elif target == "repository_profile":
        if repository is None:
            raise ValueError(
                "applying a repository_profile proposal requires the Target "
                "Repository path"
            )
        baseline_path = repository / PROFILE_RELATIVE_PATH
        if not baseline_path.is_file():
            raise ValueError(
                f"no Repository Profile at {baseline_path}; create one with "
                "`agentflow profile` before adopting profile proposals"
            )
    else:
        raise ValueError(f"unknown baseline target {target!r}")
    _apply_advisory(baseline_path, _advisory_entry(proposal))
    if now is None:
        now = datetime.now(timezone.utc)
    applied = {
        "applied_at": now.isoformat(),
        "approved_by": adoption["approved_by"],
        "baseline_path": str(baseline_path),
        "proposal_hash": content_hash,
        "proposal_id": proposal_id,
        "target": target,
        "type": "proposal_applied",
    }
    (_proposal_dir(data_dir, proposal_id) / "applied.json").write_text(
        json.dumps(applied, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return applied


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _skill_tree_hashes(root: Path) -> dict[str, str]:
    """Relative posix path -> content sha256 for every file under ``root``."""
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): _file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _skill_comparison_path(data_dir: Path) -> Path:
    return data_dir / "skills" / "comparison.json"


def _skill_adoptions_path(data_dir: Path) -> Path:
    return data_dir / "skills" / "adoptions.jsonl"


def compare_skill_baseline(
    *,
    baseline_dir: Path,
    upstream_dir: Path,
    data_dir: Path,
    now: datetime | None = None,
) -> dict:
    """Compare the local skill baseline against an upstream copy.

    Produces a per-file diff summary — ``added``, ``removed``, ``changed``,
    ``unchanged``, each with the content hashes involved — and persists it as
    the reviewable comparison record selective adoption binds to. Comparing is
    read-only with respect to both trees; nothing is ever auto-adopted.
    """
    if not upstream_dir.is_dir():
        raise ValueError(f"no upstream skill directory at {upstream_dir}")
    baseline_hashes = _skill_tree_hashes(baseline_dir)
    upstream_hashes = _skill_tree_hashes(upstream_dir)
    files = []
    for path in sorted(set(baseline_hashes) | set(upstream_hashes)):
        entry: dict = {"path": path}
        baseline_hash = baseline_hashes.get(path)
        upstream_hash = upstream_hashes.get(path)
        if baseline_hash is not None:
            entry["baseline_sha256"] = baseline_hash
        if upstream_hash is not None:
            entry["upstream_sha256"] = upstream_hash
        if baseline_hash is None:
            entry["status"] = "added"
        elif upstream_hash is None:
            entry["status"] = "removed"
        elif baseline_hash != upstream_hash:
            entry["status"] = "changed"
        else:
            entry["status"] = "unchanged"
        files.append(entry)
    if now is None:
        now = datetime.now(timezone.utc)
    record = {
        "baseline_dir": str(Path(baseline_dir).resolve()),
        "compared_at": now.isoformat(),
        "files": files,
        "type": "skill_baseline_compared",
        "upstream_dir": str(Path(upstream_dir).resolve()),
    }
    comparison_path = _skill_comparison_path(data_dir)
    comparison_path.parent.mkdir(parents=True, exist_ok=True)
    comparison_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return record


def read_skill_adoptions(data_dir: Path) -> list[dict]:
    """Return recorded skill-file adoptions in append order. Read-only."""
    path = _skill_adoptions_path(data_dir)
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def adopt_skill_file(
    *,
    baseline_dir: Path,
    upstream_dir: Path,
    data_dir: Path,
    path: str,
    approved_by: str,
    now: datetime | None = None,
) -> dict:
    """Adopt exactly one reviewed upstream skill file through the Adoption Gate.

    The adoption binds to the latest comparison record: the file must appear
    there as ``added`` or ``changed``, and both the upstream and baseline
    content must still hash to what the comparison recorded — either tree
    changing afterwards makes the review stale and refuses adoption until the
    operator re-runs the comparison. On success the exact reviewed upstream
    content replaces the baseline file, and an attributed, content-hashed
    evidence record is appended under the same advisory-lock append-sequence
    discipline as other approval logs. Files are only ever adopted one at a
    time, by name, with attribution; there is no bulk or automatic adoption.
    """
    if not approved_by:
        raise ValueError("adopting a skill file requires --approved-by")
    relative = PurePosixPath(path)
    if not path or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(
            "skill file paths must be relative to the skill baseline and "
            "must not escape it"
        )
    comparison_path = _skill_comparison_path(data_dir)
    if not comparison_path.is_file():
        raise ValueError(
            "no skill comparison record; run `agentflow skill-diff "
            "--upstream <path>` and review it before adopting"
        )
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    if comparison["upstream_dir"] != str(Path(upstream_dir).resolve()) or (
        comparison["baseline_dir"] != str(Path(baseline_dir).resolve())
    ):
        raise ValueError(
            "the recorded skill comparison covers different directories; "
            "re-run `agentflow skill-diff` for this upstream before adopting"
        )
    entry = next(
        (item for item in comparison["files"] if item["path"] == str(relative)),
        None,
    )
    if entry is None:
        raise ValueError(
            f"{relative} does not appear in the skill comparison record; "
            "re-run `agentflow skill-diff` and review it before adopting"
        )
    if entry["status"] == "unchanged":
        raise ValueError(
            f"{relative} is identical to the baseline; nothing to adopt"
        )
    if entry["status"] == "removed":
        raise ValueError(
            f"{relative} does not exist upstream; the Adoption Gate only "
            "adopts reviewed upstream content, never deletions"
        )
    upstream_path = upstream_dir / relative
    if not upstream_path.is_file():
        raise ValueError(
            f"{relative} disappeared upstream after the comparison; re-run "
            "`agentflow skill-diff` before adopting"
        )
    upstream_bytes = upstream_path.read_bytes()
    upstream_hash = hashlib.sha256(upstream_bytes).hexdigest()
    if upstream_hash != entry["upstream_sha256"]:
        raise ValueError(
            f"{relative} changed upstream after the comparison "
            f"(reviewed {entry['upstream_sha256'][:12]}, current "
            f"{upstream_hash[:12]}); the review is stale — re-run "
            "`agentflow skill-diff` and review the current content"
        )
    baseline_path = baseline_dir / relative
    baseline_hash = (
        _file_sha256(baseline_path) if baseline_path.is_file() else None
    )
    if baseline_hash != entry.get("baseline_sha256"):
        raise ValueError(
            f"the baseline copy of {relative} changed after the comparison; "
            "the review is stale — re-run `agentflow skill-diff` and review "
            "the current diff"
        )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_bytes(upstream_bytes)
    if now is None:
        now = datetime.now(timezone.utc)
    adoptions_path = _skill_adoptions_path(data_dir)
    adoptions_path.parent.mkdir(parents=True, exist_ok=True)
    adoptions_path.touch(exist_ok=True)
    with adoptions_path.open("r+", encoding="utf-8") as adoptions_file:
        fcntl.flock(adoptions_file.fileno(), fcntl.LOCK_EX)
        lines = adoptions_file.read().splitlines()
        record = {
            "adopted_at": now.isoformat(),
            "approved_by": approved_by,
            "baseline_dir": comparison["baseline_dir"],
            "path": str(relative),
            "sequence": len(lines) + 1,
            "type": "skill_file_adopted",
            "upstream_dir": comparison["upstream_dir"],
            "upstream_sha256": upstream_hash,
        }
        adoptions_file.seek(0, os.SEEK_END)
        adoptions_file.write(json.dumps(record, sort_keys=True) + "\n")
    return record
