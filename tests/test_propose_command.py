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


class ProposeAndEvaluateCommandTests(unittest.TestCase):
    """The CLI surface over proposal generation and evaluation."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.temp_path = Path(self._temp.name)
        self.data_dir = self.temp_path / "agentflow-home"
        self.environment = {
            **os.environ,
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
        }

    def _agentflow(self, *args: str):
        result = agentflow(
            *args,
            "--data-dir",
            str(self.data_dir),
            cwd=self.temp_path,
            environment=self.environment,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_propose_then_evaluate_records_a_passing_evaluation(self) -> None:
        for index in range(3):
            write_run_with_failed_check(self.data_dir, f"run-{index}")
        proposals = self._agentflow("propose")
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal["state"], "proposed")

        listed = self._agentflow("proposals")
        self.assertEqual(listed, proposals)

        evaluation = self._agentflow("evaluate", proposal["proposal_id"])
        self.assertTrue(evaluation["passed"])
        self.assertEqual(evaluation["reasons"], [])

        evaluated = self._agentflow("proposals")[0]
        self.assertEqual(evaluated["state"], "evaluated")
        self.assertEqual(evaluated["evaluation"], evaluation)

    def test_propose_reports_nothing_for_non_recurring_evidence(self) -> None:
        write_run_with_failed_check(self.data_dir, "run-0")
        self.assertEqual(self._agentflow("propose"), [])
        self.assertEqual(self._agentflow("proposals"), [])


if __name__ == "__main__":
    unittest.main()
