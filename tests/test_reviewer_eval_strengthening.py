"""Tester strengthening for the reviewer evaluation fixtures and change gate.

These tests adversarially reinforce the two safety-critical, deterministic
reviewer concerns introduced by the candidate:

* ``gate_decision`` — pinned across the *complete* disposition x severity
  matrix, so no branch (in particular ``changes_requested`` blocking on its own,
  without any blocker finding) can regress unnoticed.
* ``reviewer_fingerprint`` — proven to be genuinely *sensitive* to reviewer
  prompt and reviewer model changes. Acceptance criterion 2 ("reviewer prompt
  or model changes are gated by the fixture suite") is only met if the pinned
  digest actually moves when those inputs move; a fingerprint that ignored the
  model, or a role-scoped digest that ignored the prompt, would still pass the
  candidate's static pin test while silently letting a change slip the gate.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentflow import reviewer as reviewer_module
from agentflow.contracts import validate_review
from agentflow.reviewer import (
    GATE_APPROVE,
    GATE_BLOCKED,
    gate_decision,
    reviewer_fingerprint,
)

SEVERITIES = ("blocker", "major", "minor", "note")


def _review(disposition: str, severities: tuple[str, ...]) -> dict:
    return validate_review(
        {
            "disposition": disposition,
            "findings": [
                {"file": None, "message": f"a {sev} finding", "severity": sev}
                for sev in severities
            ],
        }
    )


class GateDecisionMatrixTests(unittest.TestCase):
    """Pin every disposition x severity combination the gate must resolve."""

    def test_approve_gates_only_without_a_blocker(self) -> None:
        # approve + no findings, and approve + any single non-blocking finding,
        # all resolve to approve.
        self.assertEqual(gate_decision(_review("approve", ())), GATE_APPROVE)
        for sev in ("major", "minor", "note"):
            with self.subTest(severity=sev):
                self.assertEqual(
                    gate_decision(_review("approve", (sev,))), GATE_APPROVE
                )
        # A blocker anywhere in the finding list overrides the approval, even
        # when it is mixed among non-blocking findings and not listed first.
        self.assertEqual(
            gate_decision(_review("approve", ("note", "minor", "blocker"))),
            GATE_BLOCKED,
        )
        self.assertEqual(
            gate_decision(_review("approve", ("blocker",))), GATE_BLOCKED
        )

    def test_changes_requested_always_blocks_regardless_of_findings(self) -> None:
        # The disposition alone blocks: no findings, only non-blocking findings,
        # and blocker findings must all gate the run. This isolates the
        # ``disposition != approve`` branch, which the candidate's fixtures only
        # exercise alongside a major/blocker finding.
        self.assertEqual(gate_decision(_review("changes_requested", ())), GATE_BLOCKED)
        for sev in SEVERITIES:
            with self.subTest(severity=sev):
                self.assertEqual(
                    gate_decision(_review("changes_requested", (sev,))),
                    GATE_BLOCKED,
                )

    def test_gate_rejects_a_malformed_review_before_deciding(self) -> None:
        # gate_decision validates first, so an unknown disposition can never be
        # silently treated as an approval.
        from agentflow.contracts import ContractError

        with self.assertRaises(ContractError):
            gate_decision({"disposition": "lgtm", "findings": []})


class ReviewerFingerprintSensitivityTests(unittest.TestCase):
    """Prove the change gate is not vacuous: it moves with prompt and model."""

    def test_fingerprint_changes_when_the_reviewer_prompt_changes(self) -> None:
        baseline = reviewer_fingerprint()
        mutated = dict(reviewer_module.ROLE_INSTRUCTIONS)
        mutated["reviewer"] = mutated["reviewer"] + " Be extra strict."
        with mock.patch.object(reviewer_module, "ROLE_INSTRUCTIONS", mutated):
            self.assertNotEqual(
                reviewer_fingerprint(),
                baseline,
                "a reviewer prompt change must move the pinned fingerprint",
            )
        # The global is restored, so the gate returns to its pinned value.
        self.assertEqual(reviewer_fingerprint(), baseline)

    def test_fingerprint_changes_when_a_reviewer_model_changes(self) -> None:
        baseline = reviewer_fingerprint()
        for adapter_name in reviewer_module.SUGGESTED_MODELS:
            with self.subTest(adapter=adapter_name):
                with mock.patch.dict(
                    reviewer_module.SUGGESTED_MODELS[adapter_name],
                    {"reviewer": "some-other-model"},
                ):
                    self.assertNotEqual(
                        reviewer_fingerprint(),
                        baseline,
                        f"a {adapter_name} reviewer model change must move the "
                        "pinned fingerprint",
                    )
        self.assertEqual(reviewer_fingerprint(), baseline)

    def test_fingerprint_is_scoped_to_the_reviewer_role(self) -> None:
        # Changing a non-reviewer role's model or instructions must NOT move the
        # reviewer's fingerprint; otherwise the gate would force spurious
        # re-reviews of the fixtures on unrelated builder/tester edits and lose
        # its meaning as reviewer-specific regression evidence.
        baseline = reviewer_fingerprint()
        for role in ("builder", "tester"):
            with self.subTest(role=role):
                with mock.patch.dict(
                    reviewer_module.SUGGESTED_MODELS["claude"],
                    {role: "unrelated-model"},
                ):
                    self.assertEqual(reviewer_fingerprint(), baseline)
                mutated = dict(reviewer_module.ROLE_INSTRUCTIONS)
                mutated[role] = mutated[role] + " unrelated change"
                with mock.patch.object(
                    reviewer_module, "ROLE_INSTRUCTIONS", mutated
                ):
                    self.assertEqual(reviewer_fingerprint(), baseline)


if __name__ == "__main__":
    unittest.main()
