from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]


def agentflow(
    *args: str,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def create_profiled_run(
    temp_path: Path,
    environment: dict[str, str],
    check: str = "python3 -c \"print('checks passed')\"",
) -> tuple[Path, Path, str]:
    repository = temp_path / "target"
    data_dir = temp_path / "agentflow-home"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "agentflow@example.test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Agentflow Test"],
        cwd=repository,
        check=True,
    )
    (repository / "README.md").write_text("# Target\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    profiled = agentflow(
        "profile",
        "--check",
        check,
        cwd=repository,
        environment=environment,
    )
    if profiled.returncode != 0:
        raise AssertionError(profiled.stderr)
    subprocess.run(
        ["git", "add", "-f", ".agentflow/repository-profile.json"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Add repository profile"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    started = agentflow(
        "start",
        "Add a health endpoint",
        "--data-dir",
        str(data_dir),
        cwd=repository,
        environment=environment,
    )
    if started.returncode != 0:
        raise AssertionError(started.stderr)
    return repository, data_dir, json.loads(started.stdout)["run_id"]


def create_built_run(
    temp_path: Path,
    environment: dict[str, str],
    check: str = "python3 -c \"print('checks passed')\"",
) -> tuple[Path, str]:
    _, data_dir, run_id = create_profiled_run(temp_path, environment, check)
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
    fixture_path.write_text(
        json.dumps(
            {
                "builder": {
                    "output": {
                        "commands_run": [],
                        "files_changed": ["README.md"],
                        "steps_completed": ["P1"],
                        "unresolved_issues": [],
                    },
                    "writes": {
                        "README.md": "# Target\n\nHealth endpoint documented.\n"
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
    if built.returncode != 0:
        raise AssertionError(built.stderr)
    return data_dir, run_id


def create_verified_run(
    temp_path: Path,
    environment: dict[str, str],
) -> tuple[Path, str]:
    data_dir, run_id = create_built_run(temp_path, environment)
    verified = agentflow(
        "advance",
        run_id,
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )
    if verified.returncode != 0:
        raise AssertionError(verified.stderr)
    return data_dir, run_id


class AdvanceCommandTests(unittest.TestCase):
    def test_advance_rejects_malformed_planner_output_without_changing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps({"planner": {"summary": "Missing required fields"}}),
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
            self.assertIn("plan fields must be exactly", advanced.stderr)
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(json.loads(status.stdout)["state"], "ready")
            self.assertFalse((data_dir / "runs" / run_id / "plan.json").exists())

    def test_advance_rejects_builder_changes_outside_the_approved_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
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
                                    "verification": "The checks pass",
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
            self.assertEqual(planned.returncode, 0, planned.stderr)
            fixture_path.write_text(
                json.dumps(
                    {
                        "builder": {
                            "output": {
                                "commands_run": [],
                                "files_changed": ["UNAPPROVED.md"],
                                "steps_completed": ["P1"],
                                "unresolved_issues": [],
                            },
                            "writes": {"UNAPPROVED.md": "not approved\n"},
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

            self.assertNotEqual(built.returncode, 0)
            self.assertIn("outside the plan", built.stderr)
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(json.loads(status.stdout)["state"], "planned")

    def test_advance_uses_validated_planner_output_from_fake_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "agentflow-home"
            fixture_path = temp_path / "adapter-fixture.json"
            repository.mkdir()
            subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "agentflow@example.test"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Agentflow Test"],
                cwd=repository,
                check=True,
            )
            (repository / "README.md").write_text("# Target\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            profiled = agentflow(
                "profile",
                "--check",
                "python3 -m unittest discover -s tests -v",
                cwd=repository,
                environment=environment,
            )
            self.assertEqual(profiled.returncode, 0, profiled.stderr)
            subprocess.run(
                ["git", "add", "-f", ".agentflow/repository-profile.json"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "Add repository profile"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            started = agentflow(
                "start",
                "Add a health endpoint",
                "--data-dir",
                str(data_dir),
                cwd=repository,
                environment=environment,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            run_id = json.loads(started.stdout)["run_id"]
            plan = {
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
            fixture_path.write_text(
                json.dumps({"planner": plan}),
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

            self.assertEqual(advanced.returncode, 0, advanced.stderr)
            self.assertEqual(
                json.loads(advanced.stdout),
                {
                    "artifact": str(data_dir.resolve() / "runs" / run_id / "plan.json"),
                    "run_id": run_id,
                    "state": "planned",
                },
            )
            self.assertEqual(
                json.loads(
                    (data_dir / "runs" / run_id / "plan.json").read_text(
                        encoding="utf-8"
                    )
                ),
                plan,
            )
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["state"], "planned")

    def test_advance_builder_commits_only_plan_approved_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            fixture_path = temp_path / "adapter-fixture.json"
            plan = {
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
            fixture_path.write_text(
                json.dumps({"planner": plan}),
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
            self.assertEqual(planned.returncode, 0, planned.stderr)
            fixture_path.write_text(
                json.dumps(
                    {
                        "builder": {
                            "output": {
                                "commands_run": [],
                                "files_changed": ["README.md"],
                                "steps_completed": ["P1"],
                                "unresolved_issues": [],
                            },
                            "writes": {
                                "README.md": "# Target\n\nHealth endpoint documented.\n"
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
            response = json.loads(built.stdout)
            self.assertEqual(response["state"], "built")
            self.assertEqual(len(response["candidate_sha"]), 40)
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["state"], "built")
            report = json.loads(
                (data_dir / "runs" / run_id / "build-report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["files_changed"], ["README.md"])

    def test_advance_runs_authoritative_checks_for_the_candidate_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_built_run(temp_path, environment)

            verified = agentflow(
                "advance",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(verified.returncode, 0, verified.stderr)
            response = json.loads(verified.stdout)
            self.assertEqual(response["state"], "verified")
            self.assertEqual(len(response["candidate_sha"]), 40)
            report = json.loads(
                (data_dir / "runs" / run_id / "checks.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["candidate_sha"], response["candidate_sha"])
            self.assertEqual(report["checks"][0]["returncode"], 0)
            self.assertIn("checks passed", report["checks"][0]["stdout"])
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["state"], "verified")

    def test_advance_fails_verification_when_a_check_dirties_the_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            dirtying_check = (
                "python3 -c \"from pathlib import Path; "
                "Path('generated.txt').write_text('dirty')\""
            )
            data_dir, run_id = create_built_run(
                temp_path,
                environment,
                dirtying_check,
            )

            verified = agentflow(
                "advance",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(json.loads(verified.stdout)["state"], "failed")
            report = json.loads(
                (data_dir / "runs" / run_id / "checks.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(report["workspace_clean"])

    def test_advance_review_stops_at_human_approval_for_verified_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_verified_run(temp_path, environment)
            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps(
                    {
                        "reviewer": {
                            "disposition": "approve",
                            "findings": [
                                {
                                    "file": None,
                                    "message": "Checks prove the documented change",
                                    "severity": "note",
                                }
                            ],
                        }
                    }
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

            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            response = json.loads(reviewed.stdout)
            self.assertEqual(response["state"], "awaiting_human")
            self.assertEqual(len(response["candidate_sha"]), 40)
            review = json.loads(
                (data_dir / "runs" / run_id / "review.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(review["disposition"], "approve")
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

    def test_codex_adapter_uses_structured_output_for_planner_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_codex = temp_path / "codex"
            fake_codex.write_text(
                """#!/usr/bin/env python3
import json
from pathlib import Path
import sys

output_path = Path(sys.argv[sys.argv.index("-o") + 1])
schema_path = Path(sys.argv[sys.argv.index("--output-schema") + 1])
json.loads(schema_path.read_text(encoding="utf-8"))
output_path.write_text(json.dumps({
    "files_to_modify": ["README.md"],
    "risks": [],
    "steps": [{
        "description": "Document the health endpoint",
        "id": "P1",
        "verification": "The authoritative checks pass"
    }],
    "summary": "Add a health endpoint"
}), encoding="utf-8")
""",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            environment = {
                **os.environ,
                "AGENTFLOW_CODEX": str(fake_codex),
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
            }
            _, data_dir, run_id = create_profiled_run(temp_path, environment)

            planned = agentflow(
                "advance",
                run_id,
                "--adapter",
                "codex",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(planned.returncode, 0, planned.stderr)
            self.assertEqual(json.loads(planned.stdout)["state"], "planned")
            self.assertTrue((data_dir / "runs" / run_id / "plan.json").is_file())

    def test_supervised_fake_flow_reaches_exact_candidate_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_verified_run(temp_path, environment)
            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps(
                    {
                        "reviewer": {
                            "disposition": "approve",
                            "findings": [],
                        }
                    }
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
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            candidate_sha = json.loads(reviewed.stdout)["candidate_sha"]

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
            approval = json.loads(approved.stdout)
            self.assertEqual(approval["state"], "human_approved")
            self.assertEqual(approval["approved_sha"], candidate_sha)
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            replayed = json.loads(status.stdout)
            self.assertEqual(replayed["state"], "human_approved")
            self.assertEqual(replayed["approved_sha"], candidate_sha)


if __name__ == "__main__":
    unittest.main()
