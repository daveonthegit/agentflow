from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


# A fake fixture that carries a Run from ready all the way to awaiting_human:
# builder writes the candidate, tester adds nothing, reviewer approves.
RECONCILE_FIXTURE = {
    "builder": {
        "output": {
            "commands_run": [],
            "files_changed": ["README.md"],
            "steps_completed": ["done"],
            "unresolved_issues": [],
        },
        "writes": {"README.md": "# Target\n\nWork item delivered.\n"},
    },
    "tester": {
        "summary": "No additional tests were required.",
        "files_changed": [],
        "findings": [],
    },
    "reviewer": {"disposition": "approve", "findings": []},
}


def agentflow(*args: str, cwd: Path, environment: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _init_repo(repository: Path, environment: dict, items: list[dict]) -> None:
    repository.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "agentflow@example.test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Agentflow Test"], cwd=repository, check=True
    )
    (repository / "README.md").write_text("# Target\n", encoding="utf-8")
    work_dir = repository / ".agentflow" / "work"
    work_dir.mkdir(parents=True)
    (work_dir / "graph.jsonl").write_text(
        "\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8"
    )
    # Profile after the Work Graph exists so the captured source fingerprint
    # covers it; otherwise Runs would see a stale profile.
    profiled = agentflow(
        "profile",
        "--check",
        "python3 -c \"print('ok')\"",
        "--test-path",
        "tests",
        cwd=repository,
        environment=environment,
    )
    if profiled.returncode != 0:
        raise AssertionError(profiled.stderr)
    subprocess.run(["git", "add", "-A", "-f"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "profile and work graph"],
        cwd=repository,
        check=True,
        capture_output=True,
    )


class ReconcileCommandTests(unittest.TestCase):
    def test_reconcile_dispatches_ready_item_to_human_gate_and_unblocks_dependent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            _init_repo(
                repository,
                environment,
                [
                    {"id": "a", "summary": "First slice", "acceptance_criteria": [], "depends_on": []},
                    {"id": "b", "summary": "Second slice", "acceptance_criteria": [], "depends_on": ["a"]},
                ],
            )
            fixture = temp_path / "fixture.json"
            fixture.write_text(json.dumps(RECONCILE_FIXTURE), encoding="utf-8")

            # Reconcile may only capture Work Items from an approved Work Graph.
            approved_graph = agentflow(
                "work",
                "approve",
                "--approved-by",
                "tester",
                "--repository",
                str(repository),
                "--data-dir",
                str(data_dir),
                cwd=repository,
                environment=environment,
            )
            self.assertEqual(approved_graph.returncode, 0, approved_graph.stderr)

            first = agentflow(
                "reconcile",
                "--repository",
                str(repository),
                "--adapter",
                "fake",
                "--adapter-fixture",
                str(fixture),
                "--data-dir",
                str(data_dir),
                cwd=repository,
                environment=environment,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            report = json.loads(first.stdout)
            # a is ready and dispatched, driven to the human gate; b is blocked.
            self.assertEqual(len(report["dispatched"]), 1)
            self.assertEqual(report["dispatched"][0]["work_item_id"], "a")
            self.assertEqual(report["dispatched"][0]["state"], "awaiting_human")
            self.assertEqual(report["blocked"], ["b"])
            run_a = report["dispatched"][0]["run_id"]

            # Reconcile never crosses the human gate: a is still awaiting_human.
            status = agentflow(
                "status", run_a, "--data-dir", str(data_dir),
                cwd=repository, environment=environment,
            )
            self.assertEqual(json.loads(status.stdout)["state"], "awaiting_human")

            # A second pass with a still unapproved must not dispatch b or a again.
            second = agentflow(
                "reconcile",
                "--repository",
                str(repository),
                "--adapter",
                "fake",
                "--adapter-fixture",
                str(fixture),
                "--data-dir",
                str(data_dir),
                cwd=repository,
                environment=environment,
            )
            second_report = json.loads(second.stdout)
            self.assertEqual(second_report["dispatched"], [])
            self.assertEqual(second_report["blocked"], ["b"])

            # Approve a; now b is unblocked and the next pass dispatches it.
            approved = agentflow(
                "approve", run_a, "--approved-by", "tester",
                "--data-dir", str(data_dir), cwd=repository, environment=environment,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)

            third = agentflow(
                "reconcile",
                "--repository",
                str(repository),
                "--adapter",
                "fake",
                "--adapter-fixture",
                str(fixture),
                "--data-dir",
                str(data_dir),
                cwd=repository,
                environment=environment,
            )
            third_report = json.loads(third.stdout)
            self.assertEqual(third_report["completed"], ["a"])
            self.assertEqual(len(third_report["dispatched"]), 1)
            self.assertEqual(third_report["dispatched"][0]["work_item_id"], "b")
            self.assertEqual(third_report["dispatched"][0]["state"], "awaiting_human")


if __name__ == "__main__":
    unittest.main()
