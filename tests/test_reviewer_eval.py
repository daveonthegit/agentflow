from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentflow.contracts import validate_review, validate_task_spec
from agentflow.reviewer import (
    GATE_APPROVE,
    GATE_BLOCKED,
    gate_decision,
    reviewer_fingerprint,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reviewer_eval"
CANDIDATE_CLASSES = ("known-good", "known-bad")
EXPECTED_GATE_BY_CLASS = {
    "known-good": GATE_APPROVE,
    "known-bad": GATE_BLOCKED,
}


def _load_manifest() -> dict:
    return json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))


def _load_candidate_fixtures() -> list[tuple[str, dict]]:
    fixtures = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        if path.name == "manifest.json":
            continue
        fixtures.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
    return fixtures


class ReviewerEvaluationFixtureTests(unittest.TestCase):
    """Exercise the reviewer approve/block gate against recorded candidates.

    The reviewer is a non-deterministic Agent Role, so each fixture records a
    known-good or known-bad candidate together with the reviewer verdict a
    correct reviewer should return. The suite pins how those verdicts resolve to
    the workflow gate and pins the reviewer prompt/model surface so it cannot
    drift without re-review.
    """

    def test_fixture_suite_is_present_and_covers_both_candidate_classes(self) -> None:
        fixtures = _load_candidate_fixtures()
        self.assertTrue(fixtures, "no reviewer evaluation fixtures found")
        seen_classes = {fixture["candidate"] for _, fixture in fixtures}
        self.assertEqual(
            seen_classes,
            set(CANDIDATE_CLASSES),
            "fixtures must include both known-good and known-bad candidates",
        )
        gates = {fixture["expected_gate"] for _, fixture in fixtures}
        self.assertEqual(
            gates,
            {GATE_APPROVE, GATE_BLOCKED},
            "fixtures must exercise both approve and block gate outcomes",
        )

    def test_each_candidate_resolves_to_its_expected_gate(self) -> None:
        for name, fixture in _load_candidate_fixtures():
            with self.subTest(fixture=name):
                self.assertIn(fixture["candidate"], CANDIDATE_CLASSES)
                # The recorded verdict and task must themselves be well formed.
                validate_task_spec(fixture["task"])
                review = validate_review(fixture["review"])
                expected_gate = fixture["expected_gate"]
                # A candidate's class fixes its gate: known-good approves,
                # known-bad blocks. This keeps a fixture honestly labelled.
                self.assertEqual(
                    expected_gate,
                    EXPECTED_GATE_BY_CLASS[fixture["candidate"]],
                    "expected_gate must match the candidate class",
                )
                self.assertEqual(
                    gate_decision(review),
                    expected_gate,
                    f"{name} did not resolve to {expected_gate}",
                )

    def test_a_blocker_finding_blocks_even_when_the_disposition_approves(self) -> None:
        # Guards the safety override the workflow depends on: the reviewer can
        # never approve past a defect it also reports as a blocker.
        review = validate_review(
            {
                "disposition": "approve",
                "findings": [
                    {
                        "file": "src/agentflow/run_kernel.py",
                        "message": "Unlocked read-then-append allows a duplicate claim.",
                        "severity": "blocker",
                    }
                ],
            }
        )
        self.assertEqual(gate_decision(review), GATE_BLOCKED)

    def test_non_blocking_findings_do_not_gate_an_approval(self) -> None:
        for severity in ("major", "minor", "note"):
            with self.subTest(severity=severity):
                review = validate_review(
                    {
                        "disposition": "approve",
                        "findings": [
                            {
                                "file": None,
                                "message": f"A {severity} observation.",
                                "severity": severity,
                            }
                        ],
                    }
                )
                self.assertEqual(gate_decision(review), GATE_APPROVE)


class ReviewerChangeGateTests(unittest.TestCase):
    """Pin the reviewer prompt and model so a change cannot bypass re-review."""

    def test_reviewer_prompt_and_model_match_the_pinned_fingerprint(self) -> None:
        pinned = _load_manifest()["reviewer_fingerprint"]
        self.assertEqual(
            reviewer_fingerprint(),
            pinned,
            "The reviewer prompt or model changed. Re-review the candidate "
            "fixtures in tests/fixtures/reviewer_eval/, update any whose verdict "
            "shifts, then refresh manifest.json to the new "
            "reviewer_fingerprint().",
        )


if __name__ == "__main__":
    unittest.main()
