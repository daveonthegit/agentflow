from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentflow.improvement import (
    DEFAULT_FIXTURES_DIR,
    KIND_RECURRING_CHECK_FAILURE,
    KIND_RECURRING_REPAIR_LOOP,
    WORKFLOW_CONFIG_FILENAME,
    adopt_skill_file,
    apply_proposal_to_baseline,
    approve_adoption,
    compare_skill_baseline,
    evaluate_proposal,
    generate_proposals,
    list_proposals,
    proposal_id_for,
    read_proposal,
    read_skill_adoptions,
)
from agentflow.repository_profile import PROFILE_RELATIVE_PATH

FAILING_CHECK = "python3 -m pytest tests/ -x -q"


def write_run_with_failed_check(
    data_dir: Path,
    run_id: str,
    *,
    command: str = FAILING_CHECK,
    failures: int = 1,
) -> None:
    """Record a Run whose profile check failed ``failures`` times."""
    run_dir = data_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    events = [
        {"run_id": run_id, "sequence": 1, "type": "run_created"},
        {"sequence": 2, "type": "build_ready", "candidate_sha": "a" * 40},
    ]
    for attempt in range(1, failures + 1):
        artifact = run_dir / f"checks-{attempt}.json"
        artifact.write_text(
            json.dumps(
                {"checks": [{"command": command, "returncode": 1}]},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        events.append(
            {
                "artifact": str(artifact),
                "sequence": len(events) + 1,
                "type": "checks_failed",
            }
        )
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def write_run_with_repair_loop(
    data_dir: Path,
    run_id: str,
    *,
    trigger: str = "tests_failed",
    repairs: int = 1,
) -> None:
    """Record a Run that ran ``repairs`` builder repairs off one trigger."""
    run_dir = data_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    events = [
        {"run_id": run_id, "sequence": 1, "type": "run_created"},
        {"sequence": 2, "type": "build_ready", "candidate_sha": "b" * 40},
    ]
    for attempt in range(1, repairs + 1):
        events.append({"sequence": len(events) + 1, "type": trigger})
        events.append(
            {
                "repair_attempt": attempt,
                "sequence": len(events) + 1,
                "type": "repair_ready",
            }
        )
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


class ProposalGenerationTests(unittest.TestCase):
    """Recurring evidence yields a proposal record; non-recurring does not."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.data_dir = Path(self._temp.name)

    def test_a_check_failing_across_three_runs_yields_a_proposal(self) -> None:
        for index in range(3):
            write_run_with_failed_check(self.data_dir, f"run-{index}")
        proposals = generate_proposals(data_dir=self.data_dir)
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal["kind"], KIND_RECURRING_CHECK_FAILURE)
        self.assertEqual(proposal["subject"], FAILING_CHECK)
        self.assertEqual(proposal["target"], "repository_profile")
        self.assertEqual(proposal["state"], "proposed")
        self.assertEqual(
            proposal["evidence"],
            [{"run_id": f"run-{index}"} for index in range(3)],
        )
        # The record is persisted, not just returned.
        self.assertEqual(
            read_proposal(
                data_dir=self.data_dir, proposal_id=proposal["proposal_id"]
            ),
            proposal,
        )

    def test_repair_loops_recurring_across_runs_yield_a_workflow_proposal(
        self,
    ) -> None:
        for index in range(3):
            write_run_with_repair_loop(self.data_dir, f"run-{index}")
        proposals = generate_proposals(data_dir=self.data_dir)
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal["kind"], KIND_RECURRING_REPAIR_LOOP)
        self.assertEqual(proposal["subject"], "tests_failed")
        self.assertEqual(proposal["target"], "workflow_config")

    def test_failures_below_the_recurrence_threshold_yield_nothing(self) -> None:
        write_run_with_failed_check(self.data_dir, "run-0")
        write_run_with_failed_check(self.data_dir, "run-1")
        self.assertEqual(generate_proposals(data_dir=self.data_dir), [])
        self.assertEqual(list_proposals(data_dir=self.data_dir), [])

    def test_repetition_inside_one_run_is_not_recurrence(self) -> None:
        # One Run failing the same check three times must not look like the
        # same check failing across three Runs.
        write_run_with_failed_check(self.data_dir, "run-0", failures=3)
        write_run_with_repair_loop(self.data_dir, "run-1", repairs=3)
        self.assertEqual(generate_proposals(data_dir=self.data_dir), [])

    def test_proposal_ids_are_stable_and_regeneration_is_idempotent(self) -> None:
        for index in range(3):
            write_run_with_failed_check(self.data_dir, f"run-{index}")
        first = generate_proposals(data_dir=self.data_dir)
        second = generate_proposals(data_dir=self.data_dir)
        self.assertEqual(first, second)
        self.assertEqual(
            first[0]["proposal_id"],
            proposal_id_for(KIND_RECURRING_CHECK_FAILURE, FAILING_CHECK),
        )

    def test_generation_writes_only_inside_the_proposals_directory(self) -> None:
        for index in range(3):
            write_run_with_failed_check(self.data_dir, f"run-{index}")
        before = {
            path
            for path in (self.data_dir / "runs").rglob("*")
        }
        generate_proposals(data_dir=self.data_dir)
        after = {
            path
            for path in (self.data_dir / "runs").rglob("*")
        }
        self.assertEqual(before, after)


class ProposalEvaluationTests(unittest.TestCase):
    """Evaluation replays the fixed fixtures and records the result."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.data_dir = Path(self._temp.name)
        for index in range(3):
            write_run_with_failed_check(self.data_dir, f"run-{index}")
        self.proposal_id = generate_proposals(data_dir=self.data_dir)[0][
            "proposal_id"
        ]

    def test_the_shipped_fixture_corpus_covers_both_proposal_kinds(self) -> None:
        cases = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(DEFAULT_FIXTURES_DIR.glob("*.json"))
        ]
        self.assertTrue(cases, "no shipped improvement evaluation fixtures")
        kinds = {case["kind"] for case in cases}
        self.assertEqual(
            kinds,
            {KIND_RECURRING_CHECK_FAILURE, KIND_RECURRING_REPAIR_LOOP},
        )
        # Historical failures must be represented: at least one case per kind
        # in which the detector must find nothing.
        silent_kinds = {
            case["kind"] for case in cases if case["expected_subjects"] == []
        }
        self.assertEqual(
            silent_kinds,
            {KIND_RECURRING_CHECK_FAILURE, KIND_RECURRING_REPAIR_LOOP},
        )

    def test_evaluation_passes_and_is_recorded_as_evidence(self) -> None:
        evaluation = evaluate_proposal(
            data_dir=self.data_dir, proposal_id=self.proposal_id
        )
        self.assertTrue(evaluation["passed"])
        self.assertEqual(evaluation["reasons"], [])
        self.assertTrue(evaluation["fixture_cases"])
        recorded = json.loads(
            (
                self.data_dir / "proposals" / self.proposal_id / "evaluation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(recorded, evaluation)
        proposal = read_proposal(
            data_dir=self.data_dir, proposal_id=self.proposal_id
        )
        self.assertEqual(proposal["state"], "evaluated")
        self.assertEqual(proposal["evaluation"], evaluation)

    def test_evaluation_fails_when_the_pattern_no_longer_recurs(self) -> None:
        # Historical evidence changed out from under the proposal: the
        # evaluation must fail with a recorded reason, not silently pass.
        for index in range(1, 3):
            events = (
                self.data_dir / "runs" / f"run-{index}" / "events.jsonl"
            )
            events.write_text(
                json.dumps(
                    {
                        "run_id": f"run-{index}",
                        "sequence": 1,
                        "type": "run_created",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        evaluation = evaluate_proposal(
            data_dir=self.data_dir, proposal_id=self.proposal_id
        )
        self.assertFalse(evaluation["passed"])
        self.assertTrue(
            any("no longer recurs" in reason for reason in evaluation["reasons"])
        )

    def test_evaluation_fails_against_a_fixture_the_detector_cannot_satisfy(
        self,
    ) -> None:
        # A fixture recording an expectation the detector does not reproduce
        # must fail the evaluation and name the case.
        fixtures = Path(self._temp.name) / "fixtures"
        fixtures.mkdir()
        (fixtures / "impossible.json").write_text(
            json.dumps(
                {
                    "name": "impossible expectation",
                    "kind": KIND_RECURRING_CHECK_FAILURE,
                    "min_runs": 3,
                    "runs": [],
                    "expected_subjects": ["never detected"],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        evaluation = evaluate_proposal(
            data_dir=self.data_dir,
            proposal_id=self.proposal_id,
            fixtures_dir=fixtures,
        )
        self.assertFalse(evaluation["passed"])
        self.assertTrue(
            any("impossible.json" in reason for reason in evaluation["reasons"])
        )

    def test_evaluating_an_unknown_proposal_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_proposal(data_dir=self.data_dir, proposal_id="missing")


class BaselineProtectionTests(unittest.TestCase):
    """No proposal changes a baseline: failing evaluation blocks, passing stops
    at the unimplemented Adoption Gate seam."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.data_dir = Path(self._temp.name)
        for index in range(3):
            write_run_with_failed_check(self.data_dir, f"run-{index}")
        self.proposal_id = generate_proposals(data_dir=self.data_dir)[0][
            "proposal_id"
        ]

    def test_an_unevaluated_proposal_cannot_change_a_baseline(self) -> None:
        with self.assertRaises(ValueError) as caught:
            apply_proposal_to_baseline(
                data_dir=self.data_dir, proposal_id=self.proposal_id
            )
        self.assertIn("has not passed evaluation", str(caught.exception))

    def test_a_failing_evaluation_cannot_change_a_baseline(self) -> None:
        fixtures = Path(self._temp.name) / "fixtures"
        fixtures.mkdir()
        (fixtures / "always-fails.json").write_text(
            json.dumps(
                {
                    "name": "always fails",
                    "kind": KIND_RECURRING_CHECK_FAILURE,
                    "min_runs": 3,
                    "runs": [],
                    "expected_subjects": ["unreachable"],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        evaluation = evaluate_proposal(
            data_dir=self.data_dir,
            proposal_id=self.proposal_id,
            fixtures_dir=fixtures,
        )
        self.assertFalse(evaluation["passed"])
        with self.assertRaises(ValueError):
            apply_proposal_to_baseline(
                data_dir=self.data_dir, proposal_id=self.proposal_id
            )

    def test_a_passing_evaluation_still_stops_at_the_adoption_gate(
        self,
    ) -> None:
        # Passing evaluation alone is not enough: without a human adoption
        # approval, the baseline stays unchanged.
        evaluation = evaluate_proposal(
            data_dir=self.data_dir, proposal_id=self.proposal_id
        )
        self.assertTrue(evaluation["passed"])
        with self.assertRaises(ValueError) as caught:
            apply_proposal_to_baseline(
                data_dir=self.data_dir, proposal_id=self.proposal_id
            )
        self.assertIn("Adoption Gate", str(caught.exception))
        # The proposal never advances past 'evaluated' here.
        self.assertEqual(
            read_proposal(
                data_dir=self.data_dir, proposal_id=self.proposal_id
            )["state"],
            "evaluated",
        )

    def test_a_failing_evaluation_cannot_be_adoption_approved(self) -> None:
        fixtures = Path(self._temp.name) / "fixtures"
        fixtures.mkdir()
        (fixtures / "always-fails.json").write_text(
            json.dumps(
                {
                    "name": "always fails",
                    "kind": KIND_RECURRING_CHECK_FAILURE,
                    "min_runs": 3,
                    "runs": [],
                    "expected_subjects": ["unreachable"],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        evaluate_proposal(
            data_dir=self.data_dir,
            proposal_id=self.proposal_id,
            fixtures_dir=fixtures,
        )
        with self.assertRaises(ValueError):
            approve_adoption(
                data_dir=self.data_dir,
                proposal_id=self.proposal_id,
                approved_by="daveonthegit",
            )

    def test_an_unevaluated_proposal_cannot_be_adoption_approved(self) -> None:
        with self.assertRaises(ValueError):
            approve_adoption(
                data_dir=self.data_dir,
                proposal_id=self.proposal_id,
                approved_by="daveonthegit",
            )

    def test_regeneration_leaves_an_evaluated_proposal_untouched(self) -> None:
        evaluate_proposal(data_dir=self.data_dir, proposal_id=self.proposal_id)
        evaluated = read_proposal(
            data_dir=self.data_dir, proposal_id=self.proposal_id
        )
        write_run_with_failed_check(self.data_dir, "run-3")
        generate_proposals(data_dir=self.data_dir)
        self.assertEqual(
            read_proposal(
                data_dir=self.data_dir, proposal_id=self.proposal_id
            ),
            evaluated,
        )


class AdoptionGateTests(unittest.TestCase):
    """A baseline changes only through an attributed, content-hashed adoption."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.data_dir = Path(self._temp.name) / "home"
        for index in range(3):
            write_run_with_failed_check(self.data_dir, f"run-{index}")
        self.proposal_id = generate_proposals(data_dir=self.data_dir)[0][
            "proposal_id"
        ]
        evaluation = evaluate_proposal(
            data_dir=self.data_dir, proposal_id=self.proposal_id
        )
        assert evaluation["passed"]
        self.repository = Path(self._temp.name) / "repo"
        self.profile_path = self.repository / PROFILE_RELATIVE_PATH
        self.profile_path.parent.mkdir(parents=True)
        self.profile_path.write_text(
            json.dumps({"checks": [["true"]], "schema_version": 1}, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    def _proposal_path(self) -> Path:
        return (
            self.data_dir / "proposals" / self.proposal_id / "proposal.json"
        )

    def test_an_approved_proposal_applies_with_attributed_evidence(self) -> None:
        adoption = approve_adoption(
            data_dir=self.data_dir,
            proposal_id=self.proposal_id,
            approved_by="daveonthegit",
        )
        self.assertEqual(adoption["type"], "adoption_approved")
        self.assertEqual(adoption["approved_by"], "daveonthegit")
        self.assertIn("approved_at", adoption)
        self.assertEqual(len(adoption["proposal_hash"]), 64)
        self.assertEqual(
            read_proposal(
                data_dir=self.data_dir, proposal_id=self.proposal_id
            )["state"],
            "approved",
        )

        applied = apply_proposal_to_baseline(
            data_dir=self.data_dir,
            proposal_id=self.proposal_id,
            repository=self.repository,
        )
        self.assertEqual(applied["type"], "proposal_applied")
        self.assertEqual(applied["approved_by"], "daveonthegit")
        self.assertEqual(applied["proposal_hash"], adoption["proposal_hash"])
        self.assertEqual(applied["baseline_path"], str(self.profile_path))
        # The evidence is persisted and drives the derived state.
        recorded = json.loads(
            (
                self.data_dir / "proposals" / self.proposal_id / "applied.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(recorded, applied)
        self.assertEqual(
            read_proposal(
                data_dir=self.data_dir, proposal_id=self.proposal_id
            )["state"],
            "applied",
        )
        # The baseline actually changed, deterministically keyed by proposal.
        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [advisory["proposal_id"] for advisory in profile["advisories"]],
            [self.proposal_id],
        )
        self.assertEqual(profile["advisories"][0]["subject"], FAILING_CHECK)
        # Untouched profile content survives the application.
        self.assertEqual(profile["checks"], [["true"]])

    def test_a_proposal_edited_after_approval_is_stale_and_refuses(self) -> None:
        approve_adoption(
            data_dir=self.data_dir,
            proposal_id=self.proposal_id,
            approved_by="daveonthegit",
        )
        record = json.loads(self._proposal_path().read_text(encoding="utf-8"))
        record["change"] = "something else entirely"
        self._proposal_path().write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # The stale approval no longer advances the derived state.
        self.assertEqual(
            read_proposal(
                data_dir=self.data_dir, proposal_id=self.proposal_id
            )["state"],
            "evaluated",
        )
        with self.assertRaises(ValueError) as caught:
            apply_proposal_to_baseline(
                data_dir=self.data_dir,
                proposal_id=self.proposal_id,
                repository=self.repository,
            )
        self.assertIn("stale", str(caught.exception))
        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        self.assertNotIn("advisories", profile)

    def test_an_applied_proposal_cannot_apply_twice(self) -> None:
        approve_adoption(
            data_dir=self.data_dir,
            proposal_id=self.proposal_id,
            approved_by="daveonthegit",
        )
        apply_proposal_to_baseline(
            data_dir=self.data_dir,
            proposal_id=self.proposal_id,
            repository=self.repository,
        )
        with self.assertRaises(ValueError) as caught:
            apply_proposal_to_baseline(
                data_dir=self.data_dir,
                proposal_id=self.proposal_id,
                repository=self.repository,
            )
        self.assertIn("already applied", str(caught.exception))

    def test_a_workflow_config_proposal_applies_to_agentflow_home(self) -> None:
        data_dir = Path(self._temp.name) / "workflow-home"
        for index in range(3):
            write_run_with_repair_loop(data_dir, f"run-{index}")
        proposal_id = generate_proposals(data_dir=data_dir)[0]["proposal_id"]
        self.assertTrue(
            evaluate_proposal(data_dir=data_dir, proposal_id=proposal_id)[
                "passed"
            ]
        )
        approve_adoption(
            data_dir=data_dir, proposal_id=proposal_id, approved_by="daveonthegit"
        )
        applied = apply_proposal_to_baseline(
            data_dir=data_dir, proposal_id=proposal_id
        )
        baseline_path = data_dir / WORKFLOW_CONFIG_FILENAME
        self.assertEqual(applied["baseline_path"], str(baseline_path))
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [advisory["subject"] for advisory in baseline["advisories"]],
            ["tests_failed"],
        )


class SkillBaselineAdoptionTests(unittest.TestCase):
    """Upstream skill changes are compared, then selectively human-adopted."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        temp_path = Path(self._temp.name)
        self.data_dir = temp_path / "home"
        self.baseline = temp_path / "skills" / "agentflow"
        self.upstream = temp_path / "upstream"
        (self.baseline / "agents").mkdir(parents=True)
        (self.baseline / "SKILL.md").write_text("# local skill\n", encoding="utf-8")
        (self.baseline / "agents" / "openai.yaml").write_text(
            "role: builder\n", encoding="utf-8"
        )
        self.upstream.mkdir()
        (self.upstream / "SKILL.md").write_text(
            "# upstream skill\n", encoding="utf-8"
        )
        (self.upstream / "NEW.md").write_text("# new guidance\n", encoding="utf-8")

    def _compare(self) -> dict:
        return compare_skill_baseline(
            baseline_dir=self.baseline,
            upstream_dir=self.upstream,
            data_dir=self.data_dir,
        )

    def _adopt(self, path: str, approved_by: str = "daveonthegit") -> dict:
        return adopt_skill_file(
            baseline_dir=self.baseline,
            upstream_dir=self.upstream,
            data_dir=self.data_dir,
            path=path,
            approved_by=approved_by,
        )

    def test_comparison_records_a_per_file_diff_summary(self) -> None:
        record = self._compare()
        self.assertEqual(record["type"], "skill_baseline_compared")
        by_path = {entry["path"]: entry for entry in record["files"]}
        self.assertEqual(by_path["SKILL.md"]["status"], "changed")
        self.assertEqual(by_path["NEW.md"]["status"], "added")
        self.assertEqual(by_path["agents/openai.yaml"]["status"], "removed")
        self.assertIn("baseline_sha256", by_path["SKILL.md"])
        self.assertIn("upstream_sha256", by_path["SKILL.md"])
        self.assertNotIn("baseline_sha256", by_path["NEW.md"])
        # The comparison is a persisted, reviewable record.
        stored = json.loads(
            (self.data_dir / "skills" / "comparison.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(stored, record)
        # Comparing adopts nothing.
        self.assertEqual(
            (self.baseline / "SKILL.md").read_text(encoding="utf-8"),
            "# local skill\n",
        )
        self.assertFalse((self.baseline / "NEW.md").exists())

    def test_selective_adoption_applies_only_the_approved_file(self) -> None:
        self._compare()
        record = self._adopt("SKILL.md")
        self.assertEqual(
            (self.baseline / "SKILL.md").read_text(encoding="utf-8"),
            "# upstream skill\n",
        )
        # Only the approved file changed; the other upstream file did not land.
        self.assertFalse((self.baseline / "NEW.md").exists())
        self.assertTrue((self.baseline / "agents" / "openai.yaml").exists())
        # The adoption evidence is attributed and content-hashed.
        self.assertEqual(record["type"], "skill_file_adopted")
        self.assertEqual(record["approved_by"], "daveonthegit")
        self.assertEqual(record["path"], "SKILL.md")
        self.assertEqual(
            record["upstream_sha256"],
            hashlib.sha256(b"# upstream skill\n").hexdigest(),
        )
        self.assertEqual(record["sequence"], 1)
        self.assertEqual(read_skill_adoptions(self.data_dir), [record])

    def test_adopting_an_added_file_creates_it_in_the_baseline(self) -> None:
        self._compare()
        self._adopt("NEW.md")
        self.assertEqual(
            (self.baseline / "NEW.md").read_text(encoding="utf-8"),
            "# new guidance\n",
        )
        self.assertEqual(len(read_skill_adoptions(self.data_dir)), 1)

    def test_adoption_refuses_without_a_comparison_record(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._adopt("SKILL.md")
        self.assertIn("skill-diff", str(caught.exception))

    def test_adoption_refuses_when_upstream_changed_after_the_review(
        self,
    ) -> None:
        self._compare()
        (self.upstream / "SKILL.md").write_text(
            "# tampered after review\n", encoding="utf-8"
        )
        with self.assertRaises(ValueError) as caught:
            self._adopt("SKILL.md")
        self.assertIn("stale", str(caught.exception))
        self.assertEqual(
            (self.baseline / "SKILL.md").read_text(encoding="utf-8"),
            "# local skill\n",
        )

    def test_adoption_refuses_identical_removed_and_escaping_paths(self) -> None:
        (self.upstream / "same.md").write_text("same\n", encoding="utf-8")
        (self.baseline / "same.md").write_text("same\n", encoding="utf-8")
        self._compare()
        with self.assertRaises(ValueError):
            self._adopt("same.md")
        with self.assertRaises(ValueError):
            self._adopt("agents/openai.yaml")  # removed upstream
        with self.assertRaises(ValueError):
            self._adopt("../outside.md")
        with self.assertRaises(ValueError):
            self._adopt("SKILL.md", approved_by="")


if __name__ == "__main__":
    unittest.main()
