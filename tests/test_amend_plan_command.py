from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

try:
    from tests.test_advance_command import (
        agentflow,
        create_profiled_run,
        create_verified_run,
    )
except ImportError:
    from test_advance_command import (
        agentflow,
        create_profiled_run,
        create_verified_run,
    )


def _events(data_dir: Path, run_id: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (data_dir / "runs" / run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def _sample_plan() -> dict:
    return {
        "files_to_modify": ["README.md"],
        "risks": ["The documentation could drift from behavior"],
        "steps": [
            {
                "description": "Document the health endpoint",
                "id": "P1",
                "verification": "The authoritative checks pass",
            }
        ],
        "summary": "Add a health endpoint",
    }


def create_planned_run(
    temp_path: Path,
    environment: dict[str, str],
) -> tuple[Path, str]:
    _, data_dir, run_id = create_profiled_run(temp_path, environment)
    fixture_path = temp_path / "adapter-fixture.json"
    fixture_path.write_text(
        json.dumps({"planner": _sample_plan()}),
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


class AmendPlanCommandTests(unittest.TestCase):
    def test_amend_from_planned_appends_event_and_status_lists_amendment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_planned_run(temp_path, environment)
            worktree = Path(
                json.loads(
                    agentflow(
                        "status",
                        run_id,
                        "--data-dir",
                        str(data_dir),
                        cwd=temp_path,
                        environment=environment,
                    ).stdout
                )["worktree"]
            )
            (worktree / "tests").mkdir(exist_ok=True)
            (worktree / "tests" / "test_x.py").write_text("", encoding="utf-8")

            amended = agentflow(
                "amend-plan",
                run_id,
                "--add-path",
                "tests/test_x.py",
                "--amended-by",
                "D",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(amended.returncode, 0, amended.stderr)
            response = json.loads(amended.stdout)
            self.assertEqual(response["state"], "planned")
            self.assertEqual(response["added_paths"], ["tests/test_x.py"])
            self.assertEqual(response["amended_by"], "D")
            self.assertNotIn("reason", response)
            events = _events(data_dir, run_id)
            amend_events = [e for e in events if e["type"] == "plan_amended"]
            self.assertEqual(len(amend_events), 1)
            self.assertEqual(amend_events[0]["added_paths"], ["tests/test_x.py"])
            self.assertEqual(amend_events[0]["amended_by"], "D")
            self.assertNotIn("reason", amend_events[0])

            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            replayed = json.loads(status.stdout)
            self.assertEqual(replayed["state"], "planned")
            self.assertEqual(
                replayed["plan_amendments"],
                [{"added_paths": ["tests/test_x.py"], "amended_by": "D"}],
            )

    def test_reason_is_recorded_when_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_planned_run(temp_path, environment)
            worktree = Path(
                json.loads(
                    agentflow(
                        "status",
                        run_id,
                        "--data-dir",
                        str(data_dir),
                        cwd=temp_path,
                        environment=environment,
                    ).stdout
                )["worktree"]
            )
            (worktree / "CHANGELOG.md").write_text("", encoding="utf-8")

            amended = agentflow(
                "amend-plan",
                run_id,
                "--add-path",
                "CHANGELOG.md",
                "--amended-by",
                "D",
                "--reason",
                "Planner omitted the changelog",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(amended.returncode, 0, amended.stderr)
            self.assertEqual(
                json.loads(amended.stdout)["reason"],
                "Planner omitted the changelog",
            )
            amend_event = next(
                e for e in _events(data_dir, run_id) if e["type"] == "plan_amended"
            )
            self.assertEqual(amend_event["reason"], "Planner omitted the changelog")
            status = json.loads(
                agentflow(
                    "status",
                    run_id,
                    "--data-dir",
                    str(data_dir),
                    cwd=temp_path,
                    environment=environment,
                ).stdout
            )
            self.assertEqual(
                status["plan_amendments"][0]["reason"],
                "Planner omitted the changelog",
            )

    def test_amended_build_enforces_and_commits_the_union_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_planned_run(temp_path, environment)
            worktree = Path(
                json.loads(
                    agentflow(
                        "status",
                        run_id,
                        "--data-dir",
                        str(data_dir),
                        cwd=temp_path,
                        environment=environment,
                    ).stdout
                )["worktree"]
            )
            (worktree / "NOTES.md").write_text("", encoding="utf-8")

            amended = agentflow(
                "amend-plan",
                run_id,
                "--add-path",
                "NOTES.md",
                "--amended-by",
                "D",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(amended.returncode, 0, amended.stderr)

            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps(
                    {
                        "builder": {
                            "output": {
                                "commands_run": [],
                                "files_changed": ["NOTES.md", "README.md"],
                                "steps_completed": ["P1"],
                                "unresolved_issues": [],
                            },
                            "writes": {
                                "NOTES.md": "notes\n",
                                "README.md": "# Target\n\nHealth endpoint.\n",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            built = agentflow(
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

            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertEqual(json.loads(built.stdout)["state"], "built")
            report = json.loads(
                (data_dir / "runs" / run_id / "build-report-1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["files_changed"], ["NOTES.md", "README.md"])

    def test_amended_build_carries_union_plan_to_adapter_request(self) -> None:
        from agentflow.workflow import advance_run

        class CapturingAdapter:
            name = "fake"

            def __init__(self) -> None:
                self.requests: list[dict] = []

            def invoke(self, *, role, request, workspace, transcript_path=None):
                self.requests.append(request)
                (workspace / "NOTES.md").write_text("notes\n", encoding="utf-8")
                (workspace / "README.md").write_text(
                    "# Target\n\nHealth endpoint.\n", encoding="utf-8"
                )
                return {
                    "commands_run": [],
                    "files_changed": ["NOTES.md", "README.md"],
                    "steps_completed": ["P1"],
                    "unresolved_issues": [],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_planned_run(temp_path, environment)
            worktree = Path(
                json.loads(
                    agentflow(
                        "status",
                        run_id,
                        "--data-dir",
                        str(data_dir),
                        cwd=temp_path,
                        environment=environment,
                    ).stdout
                )["worktree"]
            )
            (worktree / "README.md").write_text(
                "# Target\n\nHealth endpoint.\n", encoding="utf-8"
            )
            (worktree / "NOTES.md").write_text("", encoding="utf-8")
            self.assertEqual(
                agentflow(
                    "amend-plan",
                    run_id,
                    "--add-path",
                    "NOTES.md",
                    "--amended-by",
                    "D",
                    "--data-dir",
                    str(data_dir),
                    cwd=temp_path,
                    environment=environment,
                ).returncode,
                0,
            )
            (worktree / "README.md").write_text("# Target\n", encoding="utf-8")
            (worktree / "NOTES.md").unlink()

            adapter = CapturingAdapter()
            built = advance_run(run_id=run_id, data_dir=data_dir, adapter=adapter)

            self.assertEqual(built.state, "built")
            self.assertEqual(
                adapter.requests[0]["plan"]["files_to_modify"],
                ["NOTES.md", "README.md"],
            )

    def test_amend_from_changes_requested_keeps_state_and_repair_uses_union(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_verified_run(temp_path, environment)
            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps(
                    {
                        "reviewer": {
                            "disposition": "changes_requested",
                            "findings": [
                                {
                                    "file": "README.md",
                                    "message": "Need clearer docs",
                                    "severity": "major",
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            blocked = agentflow(
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
            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            self.assertEqual(json.loads(blocked.stdout)["state"], "changes_requested")

            # NOTES.md need not exist: validate_planned_paths accepts a new
            # path whose parent directory (the Workspace root) exists, and the
            # repair stage requires the Workspace to stay clean.
            amended = agentflow(
                "amend-plan",
                run_id,
                "--add-path",
                "NOTES.md",
                "--amended-by",
                "D",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(amended.returncode, 0, amended.stderr)
            self.assertEqual(json.loads(amended.stdout)["state"], "changes_requested")
            status = json.loads(
                agentflow(
                    "status",
                    run_id,
                    "--data-dir",
                    str(data_dir),
                    cwd=temp_path,
                    environment=environment,
                ).stdout
            )
            self.assertEqual(status["state"], "changes_requested")

            fixture_path.write_text(
                json.dumps(
                    {
                        "builder": {
                            "output": {
                                "commands_run": [],
                                "files_changed": ["NOTES.md", "README.md"],
                                "steps_completed": ["P1"],
                                "unresolved_issues": [],
                            },
                            "writes": {
                                "NOTES.md": "notes\n",
                                "README.md": "# Target\n\nClearer docs.\n",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            repaired = agentflow(
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
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            self.assertEqual(json.loads(repaired.stdout)["state"], "built")
            report = json.loads(
                (data_dir / "runs" / run_id / "repair-report-1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["files_changed"], ["NOTES.md", "README.md"])

    def test_amend_from_disallowed_states_errors_without_event(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}

        def assert_rejected(data_dir: Path, run_id: str, temp_path: Path, state: str) -> None:
            amended = agentflow(
                "amend-plan",
                run_id,
                "--add-path",
                "README.md",
                "--amended-by",
                "D",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertNotEqual(amended.returncode, 0)
            self.assertIn(state, amended.stderr)
            self.assertIn("cannot be amended", amended.stderr)
            # A rejected amendment appends no plan_amended event (the only new
            # events are the claim acquire/release bookkeeping pair).
            after = _events(data_dir, run_id)
            self.assertFalse(any(e["type"] == "plan_amended" for e in after))

        # verified
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir, run_id = create_verified_run(temp_path, environment)
            assert_rejected(data_dir, run_id, temp_path, "verified")

        # awaiting_human
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir, run_id = create_verified_run(temp_path, environment)
            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps({"reviewer": {"disposition": "approve", "findings": []}}),
                encoding="utf-8",
            )
            self.assertEqual(
                agentflow(
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
                ).returncode,
                0,
            )
            assert_rejected(data_dir, run_id, temp_path, "awaiting_human")

        # plan_rejected
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir, run_id = create_planned_run(temp_path, environment)
            self.assertEqual(
                agentflow(
                    "reject",
                    run_id,
                    "--rejected-by",
                    "D",
                    "--data-dir",
                    str(data_dir),
                    cwd=temp_path,
                    environment=environment,
                ).returncode,
                0,
            )
            assert_rejected(data_dir, run_id, temp_path, "plan_rejected")

    def test_invalid_paths_rejected_with_no_event(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
        for bad_path in ("/etc/passwd", "../escape.py", ""):
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                data_dir, run_id = create_planned_run(temp_path, environment)
                amended = agentflow(
                    "amend-plan",
                    run_id,
                    "--add-path",
                    bad_path,
                    "--amended-by",
                    "D",
                    "--data-dir",
                    str(data_dir),
                    cwd=temp_path,
                    environment=environment,
                )
                self.assertNotEqual(amended.returncode, 0, bad_path)
                after = _events(data_dir, run_id)
                self.assertFalse(
                    any(e["type"] == "plan_amended" for e in after), bad_path
                )

    def test_missing_required_arguments_are_usage_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_planned_run(temp_path, environment)

            missing_by = agentflow(
                "amend-plan",
                run_id,
                "--add-path",
                "README.md",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(missing_by.returncode, 2)
            self.assertIn("--amended-by", missing_by.stderr)

            missing_path = agentflow(
                "amend-plan",
                run_id,
                "--amended-by",
                "D",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(missing_path.returncode, 2)
            self.assertIn("--add-path", missing_path.stderr)

    def test_multiple_amendments_union_dedupe_and_plan_json_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_planned_run(temp_path, environment)
            plan_path = data_dir / "runs" / run_id / "plan.json"
            original_plan_bytes = plan_path.read_bytes()
            worktree = Path(
                json.loads(
                    agentflow(
                        "status",
                        run_id,
                        "--data-dir",
                        str(data_dir),
                        cwd=temp_path,
                        environment=environment,
                    ).stdout
                )["worktree"]
            )
            for name in ("A.md", "B.md"):
                (worktree / name).write_text("", encoding="utf-8")

            # First amendment: A.md and B.md (with an intra-call duplicate B.md).
            first = agentflow(
                "amend-plan",
                run_id,
                "--add-path",
                "B.md",
                "--add-path",
                "A.md",
                "--add-path",
                "B.md",
                "--amended-by",
                "D",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["added_paths"], ["A.md", "B.md"])

            # Second amendment repeats A.md and adds README.md (already planned).
            second = agentflow(
                "amend-plan",
                run_id,
                "--add-path",
                "A.md",
                "--add-path",
                "README.md",
                "--amended-by",
                "D",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(second.returncode, 0, second.stderr)

            self.assertEqual(plan_path.read_bytes(), original_plan_bytes)

            from agentflow.workflow import _effective_plan

            effective = _effective_plan(data_dir / "runs" / run_id)
            self.assertEqual(
                effective["files_to_modify"], ["A.md", "B.md", "README.md"]
            )
            self.assertEqual(
                json.loads(plan_path.read_text(encoding="utf-8"))["files_to_modify"],
                ["README.md"],
            )


if __name__ == "__main__":
    unittest.main()
