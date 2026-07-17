from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).parents[1]

try:
    from tests.test_advance_command import agentflow
    from tests.test_improvement_proposals import write_run_with_failed_check
except ImportError:  # unittest discover imports test modules without a package
    from test_advance_command import agentflow
    from test_improvement_proposals import write_run_with_failed_check


class AdoptCommandTests(unittest.TestCase):
    """The CLI surface over the Adoption Gate."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.temp_path = Path(self._temp.name)
        self.data_dir = self.temp_path / "agentflow-home"
        self.repository = self.temp_path / "repo"
        profile_path = self.repository / ".agentflow" / "repository-profile.json"
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(
            json.dumps({"checks": [["true"]], "schema_version": 1}, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        self.environment = {
            **os.environ,
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
        }

    def _agentflow(self, *args: str, expect_failure: bool = False):
        result = agentflow(
            *args,
            "--data-dir",
            str(self.data_dir),
            cwd=self.temp_path,
            environment=self.environment,
        )
        if expect_failure:
            if result.returncode == 0:
                raise AssertionError(f"expected failure, got: {result.stdout}")
            return result.stderr
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def _evaluated_proposal_id(self) -> str:
        for index in range(3):
            write_run_with_failed_check(self.data_dir, f"run-{index}")
        proposal_id = self._agentflow("propose")[0]["proposal_id"]
        evaluation = self._agentflow("evaluate", proposal_id)
        self.assertTrue(evaluation["passed"])
        return proposal_id

    def test_adopt_records_approval_and_applies_the_baseline_change(
        self,
    ) -> None:
        proposal_id = self._evaluated_proposal_id()
        response = self._agentflow(
            "adopt",
            proposal_id,
            "--approved-by",
            "daveonthegit",
            "--repository",
            str(self.repository),
        )
        self.assertEqual(response["state"], "applied")
        self.assertEqual(response["adoption"]["approved_by"], "daveonthegit")
        self.assertEqual(
            response["adoption"]["proposal_hash"],
            response["applied"]["proposal_hash"],
        )
        profile = json.loads(
            (
                self.repository / ".agentflow" / "repository-profile.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            [advisory["proposal_id"] for advisory in profile["advisories"]],
            [proposal_id],
        )
        listed = self._agentflow("proposals")[0]
        self.assertEqual(listed["state"], "applied")

    def test_adopt_refuses_an_unevaluated_proposal(self) -> None:
        for index in range(3):
            write_run_with_failed_check(self.data_dir, f"run-{index}")
        proposal_id = self._agentflow("propose")[0]["proposal_id"]
        stderr = self._agentflow(
            "adopt",
            proposal_id,
            "--approved-by",
            "daveonthegit",
            expect_failure=True,
        )
        self.assertIn("has not passed evaluation", stderr)

    def test_skill_diff_then_selective_adoption_of_one_file(self) -> None:
        baseline = self.temp_path / "skills" / "agentflow"
        baseline.mkdir(parents=True)
        (baseline / "SKILL.md").write_text("# local\n", encoding="utf-8")
        upstream = self.temp_path / "upstream"
        upstream.mkdir()
        (upstream / "SKILL.md").write_text("# upstream\n", encoding="utf-8")
        (upstream / "NEW.md").write_text("# new\n", encoding="utf-8")

        diff = self._agentflow(
            "skill-diff",
            "--upstream",
            str(upstream),
            "--repository",
            str(self.temp_path),
        )
        self.assertEqual(diff["type"], "skill_baseline_compared")
        statuses = {entry["path"]: entry["status"] for entry in diff["files"]}
        self.assertEqual(
            statuses, {"SKILL.md": "changed", "NEW.md": "added"}
        )
        # Diffing adopts nothing.
        self.assertEqual(
            (baseline / "SKILL.md").read_text(encoding="utf-8"), "# local\n"
        )

        adopted = self._agentflow(
            "adopt-skill",
            "SKILL.md",
            "--upstream",
            str(upstream),
            "--approved-by",
            "daveonthegit",
            "--repository",
            str(self.temp_path),
        )
        self.assertEqual(adopted["type"], "skill_file_adopted")
        self.assertEqual(adopted["approved_by"], "daveonthegit")
        self.assertEqual(
            (baseline / "SKILL.md").read_text(encoding="utf-8"), "# upstream\n"
        )
        # Only the approved file was adopted.
        self.assertFalse((baseline / "NEW.md").exists())

    def test_adopt_skill_requires_a_prior_diff(self) -> None:
        baseline = self.temp_path / "skills" / "agentflow"
        baseline.mkdir(parents=True)
        upstream = self.temp_path / "upstream"
        upstream.mkdir()
        (upstream / "SKILL.md").write_text("# upstream\n", encoding="utf-8")
        stderr = self._agentflow(
            "adopt-skill",
            "SKILL.md",
            "--upstream",
            str(upstream),
            "--approved-by",
            "daveonthegit",
            "--repository",
            str(self.temp_path),
            expect_failure=True,
        )
        self.assertIn("skill-diff", stderr)


if __name__ == "__main__":
    unittest.main()
