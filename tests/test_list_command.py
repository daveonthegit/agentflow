from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]

try:
    from tests.test_advance_command import agentflow, create_profiled_run
except ImportError:  # unittest discover imports test modules without a package
    from test_advance_command import agentflow, create_profiled_run


def read_first_event_line(data_dir: Path, run_id: str) -> str:
    return (
        (data_dir / "runs" / run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )


def status_without_workspace_fields(
    temp_path: Path,
    data_dir: Path,
    run_id: str,
    environment: dict[str, str],
) -> dict:
    status = agentflow(
        "status",
        run_id,
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )
    if status.returncode != 0:
        raise AssertionError(status.stderr)
    response = json.loads(status.stdout)
    return {
        key: value
        for key, value in response.items()
        if key not in ("worktree", "repository_profile_path")
    }


def write_planner_fixture(temp_path: Path) -> Path:
    fixture_path = temp_path / "adapter-fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "planner": {
                    "files_to_modify": ["README.md"],
                    "risks": [],
                    "steps": [
                        {
                            "description": "Document the health endpoint",
                            "id": "P1",
                            "verification": "The authoritative checks pass",
                        }
                    ],
                    "summary": "Add a health endpoint",
                }
            }
        ),
        encoding="utf-8",
    )
    return fixture_path


def create_three_state_runs(
    temp_path: Path,
    environment: dict[str, str],
) -> tuple[Path, dict[str, str]]:
    repository, data_dir, planned_run = create_profiled_run(temp_path, environment)
    run_ids = {"planned": planned_run}
    for state, summary in (
        ("ready", "Second task stays ready"),
        ("abandoned", "Third task gets abandoned"),
    ):
        started = agentflow(
            "start",
            summary,
            "--data-dir",
            str(data_dir),
            cwd=repository,
            environment=environment,
        )
        if started.returncode != 0:
            raise AssertionError(started.stderr)
        run_ids[state] = json.loads(started.stdout)["run_id"]
    fixture_path = write_planner_fixture(temp_path)
    planned = agentflow(
        "advance",
        run_ids["planned"],
        "--adapter",
        "fake",
        "--adapter-fixture",
        str(fixture_path),
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )
    if planned.returncode != 0:
        raise AssertionError(planned.stderr)
    abandoned = agentflow(
        "abandon",
        run_ids["abandoned"],
        "--abandoned-by",
        "list-test",
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )
    if abandoned.returncode != 0:
        raise AssertionError(abandoned.stderr)
    return data_dir, run_ids


class ListCommandTests(unittest.TestCase):
    def test_list_replays_every_run_sorted_by_first_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_ids = create_three_state_runs(temp_path, environment)

            listed = agentflow(
                "list",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(listed.returncode, 0, listed.stderr)
            entries = json.loads(listed.stdout)
            self.assertEqual(len(entries), 3)
            first_event_order = sorted(
                run_ids.values(),
                key=lambda run_id: read_first_event_line(data_dir, run_id),
            )
            self.assertEqual(
                [entry["run_id"] for entry in entries],
                first_event_order,
            )
            self.assertEqual(first_event_order, sorted(run_ids.values()))
            state_by_run_id = {
                run_id: state for state, run_id in run_ids.items()
            }
            for entry in entries:
                expected = status_without_workspace_fields(
                    temp_path,
                    data_dir,
                    entry["run_id"],
                    environment,
                )
                self.assertEqual(entry, expected)
                self.assertEqual(
                    entry["state"], state_by_run_id[entry["run_id"]]
                )
                self.assertEqual(len(entry["base_sha"]), 40)
                self.assertIsInstance(entry["summary"], str)
                self.assertIsInstance(entry["repository"], str)
                self.assertNotIn("worktree", entry)
                self.assertNotIn("candidate_sha", entry)
                self.assertNotIn("approved_sha", entry)
                self.assertNotIn("acceptance_criteria", entry)
                self.assertNotIn("source", entry)

    def test_list_omits_source_and_criteria_even_when_status_includes_them(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            repository, data_dir, run_id = create_profiled_run(temp_path, environment)
            started = agentflow(
                "start",
                "Criteria stay out of list",
                "--acceptance-criterion",
                "checks pass",
                "--data-dir",
                str(data_dir),
                cwd=repository,
                environment=environment,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            criteria_run = json.loads(started.stdout)["run_id"]

            status = agentflow(
                "status",
                criteria_run,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn(
                "acceptance_criteria", json.loads(status.stdout)
            )

            listed = agentflow(
                "list",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            entry = next(
                item
                for item in json.loads(listed.stdout)
                if item["run_id"] == criteria_run
            )
            self.assertNotIn("acceptance_criteria", entry)
            self.assertNotIn("source", entry)
            self.assertNotIn("worktree", entry)

    def test_list_state_option_filters_to_one_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_ids = create_three_state_runs(temp_path, environment)

            listed = agentflow(
                "list",
                "--state",
                "abandoned",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(listed.returncode, 0, listed.stderr)
            entries = json.loads(listed.stdout)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["run_id"], run_ids["abandoned"])
            self.assertEqual(entries[0]["state"], "abandoned")

    def test_list_prints_an_empty_array_for_an_empty_agentflow_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            missing_home = temp_path / "never-created"
            existing_home = temp_path / "existing-home"
            existing_home.mkdir()

            for data_dir in (missing_home, existing_home):
                listed = agentflow(
                    "list",
                    "--data-dir",
                    str(data_dir),
                    cwd=temp_path,
                    environment=environment,
                )
                self.assertEqual(listed.returncode, 0, listed.stderr)
                self.assertEqual(json.loads(listed.stdout), [])


if __name__ == "__main__":
    unittest.main()
