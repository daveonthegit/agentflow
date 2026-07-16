from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentflow.run_kernel import acquire_claim, release_claim  # noqa: E402

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


def write_reviewer_fixture(temp_path: Path) -> Path:
    fixture_path = temp_path / "adapter-fixture.json"
    fixture_path.write_text(
        json.dumps({"reviewer": {"disposition": "approve", "findings": []}}),
        encoding="utf-8",
    )
    return fixture_path


class StageClaimTests(unittest.TestCase):
    def test_advance_is_rejected_while_an_unexpired_claim_is_held(self) -> None:
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
            expires_at = read_events(data_dir, run_id)[-1]["expires_at"]
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

            self.assertNotEqual(advanced.returncode, 0)
            self.assertIn("other-process", advanced.stderr)
            self.assertIn(expires_at, advanced.stderr)
            self.assertEqual(events_path.read_text(encoding="utf-8"), events_before)
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["state"], "ready")

    def test_advance_recovers_an_expired_claim_with_expiry_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            acquire_claim(
                data_dir=data_dir,
                run_id=run_id,
                holder="expired-process",
                lease_seconds=60,
                now=datetime.now(timezone.utc) - timedelta(seconds=7200),
            )
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

            self.assertEqual(advanced.returncode, 0, advanced.stderr)
            self.assertEqual(json.loads(advanced.stdout)["state"], "planned")
            events = read_events(data_dir, run_id)
            self.assertEqual(
                [event["type"] for event in events[-4:]],
                ["claim_expired", "claim_acquired", "plan_ready", "claim_released"],
            )
            self.assertEqual(events[-4]["holder"], "expired-process")
            self.assertNotEqual(events[-3]["holder"], "expired-process")
            self.assertEqual(events[-1]["holder"], events[-3]["holder"])

    def test_release_by_a_superseded_holder_appends_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            acquire_claim(
                data_dir=data_dir,
                run_id=run_id,
                holder="stale-process",
                lease_seconds=60,
                now=datetime.now(timezone.utc) - timedelta(seconds=7200),
            )
            acquire_claim(
                data_dir=data_dir,
                run_id=run_id,
                holder="new-process",
                lease_seconds=100000,
            )
            events_before = read_events(data_dir, run_id)
            self.assertEqual(events_before[-1]["type"], "claim_acquired")
            self.assertEqual(events_before[-1]["holder"], "new-process")

            release_claim(data_dir=data_dir, run_id=run_id, holder="stale-process")

            events_after = read_events(data_dir, run_id)
            self.assertEqual(events_after, events_before)

            release_claim(data_dir=data_dir, run_id=run_id, holder="new-process")

            released = read_events(data_dir, run_id)[-1]
            self.assertEqual(released["type"], "claim_released")
            self.assertEqual(released["holder"], "new-process")

    def test_claim_events_keep_sequence_integrity_and_change_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_tested_run(temp_path, environment)
            fixture_path = write_reviewer_fixture(temp_path)
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

            events = read_events(data_dir, run_id)
            for line_number, event in enumerate(events, start=1):
                self.assertEqual(event["sequence"], line_number)
            event_types = {event["type"] for event in events}
            self.assertIn("claim_acquired", event_types)
            self.assertIn("claim_released", event_types)
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["state"], "awaiting_human")

    def test_claim_lease_override_is_recorded_in_the_claim_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            fixture_path = write_planner_fixture(temp_path)

            advanced = agentflow(
                "advance",
                run_id,
                "--adapter",
                "fake",
                "--adapter-fixture",
                str(fixture_path),
                "--claim-lease-seconds",
                "9000",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(advanced.returncode, 0, advanced.stderr)
            claim = next(
                event
                for event in read_events(data_dir, run_id)
                if event["type"] == "claim_acquired"
            )
            acquired_at = datetime.fromisoformat(claim["acquired_at"])
            expires_at = datetime.fromisoformat(claim["expires_at"])
            self.assertEqual(expires_at - acquired_at, timedelta(seconds=9000))

    def test_full_fake_flow_reaches_approval_with_claims_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_tested_run(temp_path, environment)
            fixture_path = write_reviewer_fixture(temp_path)
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
            self.assertEqual(json.loads(reviewed.stdout)["state"], "awaiting_human")
            events = read_events(data_dir, run_id)
            acquired = [e for e in events if e["type"] == "claim_acquired"]
            released = [e for e in events if e["type"] == "claim_released"]
            self.assertEqual(len(acquired), 5)
            self.assertEqual(len(released), 5)

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
            self.assertEqual(json.loads(approved.stdout)["state"], "human_approved")
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["state"], "human_approved")


if __name__ == "__main__":
    unittest.main()
