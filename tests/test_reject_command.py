from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentflow.run_kernel import acquire_claim  # noqa: E402

try:
    from tests.test_advance_command import (
        agentflow,
        create_profiled_run,
        create_tested_run,
    )
except ImportError:
    from test_advance_command import (
        agentflow,
        create_profiled_run,
        create_tested_run,
    )


def read_events(data_dir: Path, run_id: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (data_dir / "runs" / run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def plan_run(
    temp_path: Path,
    environment: dict[str, str],
) -> tuple[Path, str]:
    _, data_dir, run_id = create_profiled_run(temp_path, environment)
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
    planned = agentflow(
        "advance",
        run_id,
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
    return data_dir, run_id


def await_human(
    temp_path: Path,
    environment: dict[str, str],
) -> tuple[Path, str, str]:
    data_dir, run_id = create_tested_run(temp_path, environment)
    fixture_path = temp_path / "adapter-fixture.json"
    fixture_path.write_text(
        json.dumps(
            {"reviewer": {"disposition": "approve", "findings": []}}
        ),
        encoding="utf-8",
    )
    reviewed = agentflow(
        "advance",
        run_id,
        "--adapter",
        "fake",
        "--adapter-fixture",
        str(fixture_path),
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )
    if reviewed.returncode != 0:
        raise AssertionError(reviewed.stderr)
    response = json.loads(reviewed.stdout)
    assert response["state"] == "awaiting_human"
    return data_dir, run_id, response["candidate_sha"]


class RejectCommandTests(unittest.TestCase):
    def test_reject_from_planned_appends_plan_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = plan_run(temp_path, environment)
            rejected = agentflow(
                "reject",
                run_id,
                "--rejected-by",
                "planner-human",
                "--reason",
                "scope too large",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(rejected.returncode, 0, rejected.stderr)
            response = json.loads(rejected.stdout)
            self.assertEqual(response["state"], "plan_rejected")
            self.assertEqual(response["rejected_by"], "planner-human")
            self.assertEqual(response["reason"], "scope too large")
            events = read_events(data_dir, run_id)
            self.assertTrue(
                any(event["type"] == "plan_rejected" for event in events)
            )
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(json.loads(status.stdout)["state"], "plan_rejected")
            listed = agentflow(
                "list",
                "--state",
                "plan_rejected",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(
                json.loads(listed.stdout)[0]["run_id"], run_id
            )
            watched = agentflow(
                "watch",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(watched.returncode, 0, watched.stderr)
            self.assertIn(f"run {run_id} plan_rejected", watched.stdout)

    def test_reject_from_awaiting_human_binds_candidate_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, candidate_sha = await_human(temp_path, environment)
            rejected = agentflow(
                "reject",
                run_id,
                "--rejected-by",
                "review-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(rejected.returncode, 0, rejected.stderr)
            response = json.loads(rejected.stdout)
            self.assertEqual(response["state"], "human_rejected")
            self.assertEqual(response["rejected_sha"], candidate_sha)
            self.assertNotIn("reason", response)
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(json.loads(status.stdout)["state"], "human_rejected")
            watched = agentflow(
                "watch",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertIn(f"run {run_id} human_rejected", watched.stdout)

    def test_reject_fails_from_invalid_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            rejected = agentflow(
                "reject",
                run_id,
                "--rejected-by",
                "someone",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("cannot be rejected from state ready", rejected.stderr)

    def test_reject_is_claim_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = plan_run(temp_path, environment)
            acquire_claim(
                data_dir=data_dir,
                run_id=run_id,
                holder="other-process",
                lease_seconds=600,
            )
            rejected = agentflow(
                "reject",
                run_id,
                "--rejected-by",
                "someone",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("other-process", rejected.stderr)

    def test_rejected_runs_block_mutation_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = plan_run(temp_path, environment)
            rejected = agentflow(
                "reject",
                run_id,
                "--rejected-by",
                "planner-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(rejected.returncode, 0, rejected.stderr)

            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps(
                    {
                        "builder": {
                            "commands_run": [],
                            "files_changed": [],
                            "steps_completed": [],
                            "unresolved_issues": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            advanced = agentflow(
                "advance",
                run_id,
                "--adapter",
                "fake",
                "--adapter-fixture",
                str(fixture_path),
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertNotEqual(advanced.returncode, 0)
            self.assertIn("cannot advance from state plan_rejected", advanced.stderr)

            approved = agentflow(
                "approve",
                run_id,
                "--approved-by",
                "someone",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertNotEqual(approved.returncode, 0)
            self.assertIn("cannot be approved from state plan_rejected", approved.stderr)

            abandoned = agentflow(
                "abandon",
                run_id,
                "--abandoned-by",
                "someone",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertNotEqual(abandoned.returncode, 0)
            self.assertIn(
                "cannot be abandoned from state plan_rejected", abandoned.stderr
            )

            rebased = agentflow(
                "rebase",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertNotEqual(rebased.returncode, 0)
            self.assertIn("cannot be rebased from state plan_rejected", rebased.stderr)

            second = agentflow(
                "reject",
                run_id,
                "--rejected-by",
                "someone-else",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertIn(
                "cannot be rejected from state plan_rejected", second.stderr
            )


if __name__ == "__main__":
    unittest.main()
