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
except ImportError:  # unittest discover imports test modules without a package
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


def read_state(
    temp_path: Path,
    data_dir: Path,
    run_id: str,
    environment: dict[str, str],
) -> str:
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
    return json.loads(status.stdout)["state"]


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


def abandon(
    temp_path: Path,
    data_dir: Path,
    run_id: str,
    environment: dict[str, str],
    *extra: str,
):
    return agentflow(
        "abandon",
        run_id,
        "--abandoned-by",
        "abandon-test",
        *extra,
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )


class AbandonCommandTests(unittest.TestCase):
    def test_abandon_appends_a_terminal_event_that_status_replays(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            _, data_dir, run_id = create_profiled_run(temp_path, environment)

            abandoned = abandon(
                temp_path,
                data_dir,
                run_id,
                environment,
                "--reason",
                "superseded by another run",
            )

            self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
            self.assertEqual(
                json.loads(abandoned.stdout),
                {
                    "abandoned_by": "abandon-test",
                    "reason": "superseded by another run",
                    "run_id": run_id,
                    "state": "abandoned",
                },
            )
            events = read_events(data_dir, run_id)
            for line_number, event in enumerate(events, start=1):
                self.assertEqual(event["sequence"], line_number)
            event = next(
                event for event in events if event["type"] == "run_abandoned"
            )
            self.assertEqual(event["abandoned_by"], "abandon-test")
            self.assertEqual(event["reason"], "superseded by another run")
            self.assertEqual(
                read_state(temp_path, data_dir, run_id, environment),
                "abandoned",
            )

    def test_advance_and_approve_fail_on_an_abandoned_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            abandoned = abandon(temp_path, data_dir, run_id, environment)
            self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
            fixture_path = write_planner_fixture(temp_path)

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
            approved = agentflow(
                "approve",
                run_id,
                "--approved-by",
                "integration-test-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertNotEqual(advanced.returncode, 0)
            self.assertIn(
                f"run {run_id} cannot advance from state abandoned",
                advanced.stderr,
            )
            self.assertNotEqual(approved.returncode, 0)
            self.assertIn(
                f"run {run_id} cannot be approved from state abandoned",
                approved.stderr,
            )
            events = read_events(data_dir, run_id)
            self.assertEqual(
                len([e for e in events if e["type"] == "run_abandoned"]), 1
            )
            self.assertEqual(
                read_state(temp_path, data_dir, run_id, environment),
                "abandoned",
            )

    def test_abandon_is_rejected_while_an_unexpired_claim_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            acquire_claim(
                data_dir=data_dir,
                run_id=run_id,
                holder="other-process",
                lease_seconds=100000,
            )
            events_path = data_dir / "runs" / run_id / "events.jsonl"
            events_before = events_path.read_text(encoding="utf-8")

            abandoned = abandon(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(abandoned.returncode, 0)
            self.assertIn("other-process", abandoned.stderr)
            self.assertEqual(
                events_path.read_text(encoding="utf-8"), events_before
            )
            self.assertEqual(
                read_state(temp_path, data_dir, run_id, environment),
                "ready",
            )

    def test_abandon_fails_on_an_already_abandoned_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            first = abandon(temp_path, data_dir, run_id, environment)
            self.assertEqual(first.returncode, 0, first.stderr)

            second = abandon(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(second.returncode, 0)
            self.assertIn(
                f"run {run_id} cannot be abandoned from state abandoned",
                second.stderr,
            )
            events = read_events(data_dir, run_id)
            self.assertEqual(
                len([e for e in events if e["type"] == "run_abandoned"]), 1
            )

    def test_abandon_fails_on_a_human_approved_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_tested_run(temp_path, environment)
            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps({"reviewer": {"disposition": "approve", "findings": []}}),
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
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            approved = agentflow(
                "approve",
                run_id,
                "--approved-by",
                "integration-test-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)

            abandoned = abandon(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(abandoned.returncode, 0)
            self.assertIn(
                f"run {run_id} cannot be abandoned from state human_approved",
                abandoned.stderr,
            )
            self.assertEqual(
                read_state(temp_path, data_dir, run_id, environment),
                "human_approved",
            )


if __name__ == "__main__":
    unittest.main()
