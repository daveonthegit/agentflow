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


# A tester fixture that writes no files: the fake adapter returns this report
# directly (no "output"/"writes" wrapping), so the tester stage records it and
# advances to `tested` without re-running checks.
TESTER_NO_CHANGE_FIXTURE = {
    "tester": {
        "summary": "No additional tests were required for this candidate.",
        "files_changed": [],
        "findings": [],
    }
}

# A check that runs every Python file the tester may add under tests/, so a
# passing test keeps checks green and a failing test turns them red. Before the
# tester writes anything the directory is empty, so it is a no-op at built.
TEST_RUNNING_CHECK = (
    "python3 -c \"import glob, subprocess, sys; "
    "[subprocess.run([sys.executable, f], check=True) "
    "for f in sorted(glob.glob('tests/*.py'))]\""
)

TESTER_PASSING_FIXTURE = {
    "tester": {
        "output": {
            "summary": "Added a passing regression test under the test paths.",
            "files_changed": ["tests/test_health.py"],
            "findings": [],
        },
        "writes": {"tests/test_health.py": "print('health endpoint documented')\n"},
    }
}

TESTER_FAILING_FIXTURE = {
    "tester": {
        "output": {
            "summary": "Added a failing test exposing a suspected defect.",
            "files_changed": ["tests/test_regression.py"],
            "findings": [
                {
                    "file": "tests/test_regression.py",
                    "message": "The candidate does not satisfy the acceptance criteria",
                    "severity": "blocker",
                }
            ],
        },
        "writes": {"tests/test_regression.py": "raise SystemExit(1)\n"},
    }
}

TESTER_OUT_OF_PATH_FIXTURE = {
    "tester": {
        "output": {
            "summary": "Attempted to edit production code.",
            "files_changed": ["README.md"],
            "findings": [],
        },
        "writes": {"README.md": "# Target\n\nTester touched production code.\n"},
    }
}


def _events(data_dir: Path, run_id: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (data_dir / "runs" / run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def create_profiled_run(
    temp_path: Path,
    environment: dict[str, str],
    check: str = "python3 -c \"print('checks passed')\"",
    test_paths: list[str] | None = ("tests",),
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
    profile_arguments = ["profile", "--check", check]
    for test_path in test_paths or ():
        profile_arguments.extend(["--test-path", test_path])
    profiled = agentflow(
        *profile_arguments,
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
    test_paths: list[str] | None = ("tests",),
) -> tuple[Path, str]:
    _, data_dir, run_id = create_profiled_run(
        temp_path, environment, check, test_paths
    )
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
    check: str = "python3 -c \"print('checks passed')\"",
    test_paths: list[str] | None = ("tests",),
) -> tuple[Path, str]:
    data_dir, run_id = create_built_run(temp_path, environment, check, test_paths)
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


def advance_tester(
    temp_path: Path,
    data_dir: Path,
    run_id: str,
    environment: dict[str, str],
    fixture: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    """Advance a verified Run through the tester stage.

    The default fixture writes no files, so the stage reaches `tested` without
    re-running checks. Callers exercising the writing paths pass their own.
    """
    fixture_path = temp_path / "tester-fixture.json"
    fixture_path.write_text(
        json.dumps(TESTER_NO_CHANGE_FIXTURE if fixture is None else fixture),
        encoding="utf-8",
    )
    tested = agentflow(
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
    if tested.returncode != 0:
        raise AssertionError(tested.stderr)
    return tested


def create_tested_run(
    temp_path: Path,
    environment: dict[str, str],
) -> tuple[Path, str]:
    data_dir, run_id = create_verified_run(temp_path, environment)
    advance_tester(temp_path, data_dir, run_id, environment)
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
                (data_dir / "runs" / run_id / "build-report-1.json").read_text(
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
                (data_dir / "runs" / run_id / "checks-1.json").read_text(
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
                (data_dir / "runs" / run_id / "checks-1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(report["workspace_clean"])

    def test_advance_review_stops_at_human_approval_for_verified_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_tested_run(temp_path, environment)
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
                (data_dir / "runs" / run_id / "review-1.json").read_text(
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

    def test_claude_adapter_uses_structured_output_for_planner_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_claude = temp_path / "claude"
            fake_claude.write_text(
                """#!/usr/bin/env python3
import json
import sys

arguments = sys.argv[1:]


def value(flag):
    return arguments[arguments.index(flag) + 1]


assert "--print" in arguments
assert value("--output-format") == "stream-json"
assert "--include-partial-messages" not in arguments
assert value("--tools") == "Read,Grep,Glob"
assert value("--permission-mode") == "dontAsk"
assert "planner" in sys.stdin.read()
json.loads(value("--json-schema"))
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"type": "assistant", "message": {"content": "planning"}}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "planned",
    "structured_output": {
        "files_to_modify": ["README.md"],
        "risks": [],
        "steps": [{
            "description": "Document the health endpoint",
            "id": "P1",
            "verification": "The authoritative checks pass"
        }],
        "summary": "Add a health endpoint"
    }
}))
""",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            environment = {
                **os.environ,
                "AGENTFLOW_CLAUDE": str(fake_claude),
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
            }
            _, data_dir, run_id = create_profiled_run(temp_path, environment)

            planned = agentflow(
                "advance",
                run_id,
                "--adapter",
                "claude",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(planned.returncode, 0, planned.stderr)
            self.assertEqual(json.loads(planned.stdout)["state"], "planned")
            plan = json.loads(
                (data_dir / "runs" / run_id / "plan.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(plan["files_to_modify"], ["README.md"])

    def test_claude_adapter_builder_writes_only_inside_the_workspace(self) -> None:
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
            self.assertEqual(planned.returncode, 0, planned.stderr)
            fake_claude = temp_path / "claude"
            fake_claude.write_text(
                """#!/usr/bin/env python3
import json
from pathlib import Path
import sys

arguments = sys.argv[1:]


def value(flag):
    return arguments[arguments.index(flag) + 1]


assert value("--output-format") == "stream-json"
assert "--include-partial-messages" not in arguments
assert value("--permission-mode") == "acceptEdits"
assert value("--allowedTools") == "Bash"
assert "builder" in sys.stdin.read()
Path("README.md").write_text(
    "# Target\\n\\nHealth endpoint documented.\\n", encoding="utf-8"
)
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"type": "assistant", "message": {"content": "building"}}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "built",
    "structured_output": {
        "commands_run": [],
        "files_changed": ["README.md"],
        "steps_completed": ["P1"],
        "unresolved_issues": []
    }
}))
""",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            environment = {**environment, "AGENTFLOW_CLAUDE": str(fake_claude)}

            built = agentflow(
                "advance",
                run_id,
                "--adapter",
                "claude",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(built.returncode, 0, built.stderr)
            response = json.loads(built.stdout)
            self.assertEqual(response["state"], "built")
            self.assertEqual(len(response["candidate_sha"]), 40)
            report = json.loads(
                (data_dir / "runs" / run_id / "build-report-1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["files_changed"], ["README.md"])

    def test_claude_adapter_rejects_error_envelope_without_changing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_claude = temp_path / "claude"
            fake_claude.write_text(
                """#!/usr/bin/env python3
import json

print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "num_turns": 3,
    "total_cost_usd": 0.42,
    "result": "the session failed before structured output"
}))
""",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            environment = {
                **os.environ,
                "AGENTFLOW_CLAUDE": str(fake_claude),
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
            }
            _, data_dir, run_id = create_profiled_run(temp_path, environment)

            planned = agentflow(
                "advance",
                run_id,
                "--adapter",
                "claude",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertNotEqual(planned.returncode, 0)
            self.assertIn("Claude adapter reported failure", planned.stderr)
            self.assertIn("error_during_execution", planned.stderr)
            self.assertIn("num_turns", planned.stderr)
            self.assertIn("3", planned.stderr)
            self.assertIn("total_cost_usd", planned.stderr)
            self.assertIn("0.42", planned.stderr)
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

    def test_claude_adapter_nonzero_exit_includes_result_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_claude = temp_path / "claude"
            fake_claude.write_text(
                """#!/usr/bin/env python3
import json
import sys

print(json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "num_turns": 2,
    "total_cost_usd": 1.25,
    "result": "aborted"
}))
print("stderr boom", file=sys.stderr)
raise SystemExit(7)
""",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            environment = {
                **os.environ,
                "AGENTFLOW_CLAUDE": str(fake_claude),
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
            }
            _, data_dir, run_id = create_profiled_run(temp_path, environment)

            planned = agentflow(
                "advance",
                run_id,
                "--adapter",
                "claude",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertNotEqual(planned.returncode, 0)
            self.assertIn("Claude adapter failed for role planner", planned.stderr)
            self.assertIn("stderr boom", planned.stderr)
            self.assertIn("error_during_execution", planned.stderr)
            self.assertIn('"num_turns": 2', planned.stderr)
            self.assertIn('"total_cost_usd": 1.25', planned.stderr)

    def test_supervised_fake_flow_reaches_exact_candidate_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_tested_run(temp_path, environment)
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

    def test_successful_repair_then_recheck_and_rereview(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_tested_run(temp_path, environment)
            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps(
                    {
                        "reviewer": {
                            "disposition": "changes_requested",
                            "findings": [
                                {
                                    "file": "README.md",
                                    "message": "Need clearer health endpoint docs",
                                    "severity": "major",
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
            self.assertEqual(json.loads(reviewed.stdout)["state"], "changes_requested")
            first_review = (data_dir / "runs" / run_id / "review-1.json").read_text(
                encoding="utf-8"
            )
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
                                "README.md": (
                                    "# Target\n\nHealth endpoint documented clearly.\n"
                                )
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
            repaired_response = json.loads(repaired.stdout)
            self.assertEqual(repaired_response["state"], "built")
            repair_sha = repaired_response["candidate_sha"]
            self.assertTrue(
                (data_dir / "runs" / run_id / "repair-report-1.json").is_file()
            )
            self.assertEqual(
                (data_dir / "runs" / run_id / "review-1.json").read_text(
                    encoding="utf-8"
                ),
                first_review,
            )

            rechecked = agentflow(
                "advance",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(rechecked.returncode, 0, rechecked.stderr)
            self.assertEqual(json.loads(rechecked.stdout)["state"], "verified")
            self.assertEqual(json.loads(rechecked.stdout)["candidate_sha"], repair_sha)
            checks = json.loads(
                (data_dir / "runs" / run_id / "checks-2.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(checks["candidate_sha"], repair_sha)
            self.assertTrue((data_dir / "runs" / run_id / "checks-1.json").is_file())

            advance_tester(temp_path, data_dir, run_id, environment)
            fixture_path.write_text(
                json.dumps({"reviewer": {"disposition": "approve", "findings": []}}),
                encoding="utf-8",
            )
            rereviewed = agentflow(
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
            self.assertEqual(rereviewed.returncode, 0, rereviewed.stderr)
            self.assertEqual(json.loads(rereviewed.stdout)["state"], "awaiting_human")
            self.assertTrue((data_dir / "runs" / run_id / "review-2.json").is_file())

    def test_repair_exhaustion_after_two_repairs_without_third_model_invoke(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_tested_run(temp_path, environment)
            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps(
                    {
                        "reviewer": {
                            "disposition": "changes_requested",
                            "findings": [
                                {
                                    "file": "README.md",
                                    "message": "Need clearer health endpoint docs",
                                    "severity": "major",
                                }
                            ],
                        }
                    }
                ),
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

            for attempt in (1, 2):
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
                                    "README.md": (
                                        f"# Target\n\nRepair attempt {attempt}.\n"
                                    )
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
                self.assertEqual(
                    agentflow(
                        "advance",
                        run_id,
                        "--data-dir",
                        str(data_dir),
                        cwd=temp_path,
                        environment=environment,
                    ).returncode,
                    0,
                )
                advance_tester(temp_path, data_dir, run_id, environment)
                fixture_path.write_text(
                    json.dumps(
                        {
                            "reviewer": {
                                "disposition": "changes_requested",
                                "findings": [
                                    {
                                        "file": "README.md",
                                        "message": (
                                            f"Still needs work after repair {attempt}"
                                        ),
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
                self.assertEqual(
                    json.loads(blocked.stdout)["state"], "changes_requested"
                )

            fixture_path.write_text(
                json.dumps(
                    {
                        "builder": {
                            "output": {
                                "commands_run": ["should-not-run"],
                                "files_changed": ["README.md"],
                                "steps_completed": ["P1"],
                                "unresolved_issues": [],
                            },
                            "writes": {
                                "README.md": "# Target\n\nShould not be written.\n"
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
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
            before_readme = (worktree / "README.md").read_text(encoding="utf-8")
            exhausted = agentflow(
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
            self.assertEqual(exhausted.returncode, 0, exhausted.stderr)
            self.assertEqual(json.loads(exhausted.stdout)["state"], "failed")
            events = [
                json.loads(line)
                for line in (data_dir / "runs" / run_id / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                sum(1 for event in events if event["type"] == "repair_ready"), 2
            )
            self.assertTrue(
                any(event["type"] == "repair_exhausted" for event in events)
            )
            self.assertFalse(
                (data_dir / "runs" / run_id / "repair-report-3.json").exists()
            )
            self.assertEqual(
                (worktree / "README.md").read_text(encoding="utf-8"), before_readme
            )
            self.assertNotIn("Should not be written", before_readme)

    def test_attempt_artifacts_are_immutable_across_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_tested_run(temp_path, environment)
            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps(
                    {
                        "reviewer": {
                            "disposition": "changes_requested",
                            "findings": [
                                {
                                    "file": "README.md",
                                    "message": "Need clearer health endpoint docs",
                                    "severity": "major",
                                }
                            ],
                        }
                    }
                ),
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
            run_dir = data_dir / "runs" / run_id
            original_build = (run_dir / "build-report-1.json").read_text(
                encoding="utf-8"
            )
            original_checks = (run_dir / "checks-1.json").read_text(encoding="utf-8")
            original_review = (run_dir / "review-1.json").read_text(encoding="utf-8")
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
                                "README.md": "# Target\n\nRepaired documentation.\n"
                            },
                        }
                    }
                ),
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
            self.assertEqual(
                agentflow(
                    "advance",
                    run_id,
                    "--data-dir",
                    str(data_dir),
                    cwd=temp_path,
                    environment=environment,
                ).returncode,
                0,
            )
            advance_tester(temp_path, data_dir, run_id, environment)
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
            self.assertEqual(
                (run_dir / "build-report-1.json").read_text(encoding="utf-8"),
                original_build,
            )
            self.assertEqual(
                (run_dir / "checks-1.json").read_text(encoding="utf-8"),
                original_checks,
            )
            self.assertEqual(
                (run_dir / "review-1.json").read_text(encoding="utf-8"),
                original_review,
            )
            self.assertNotEqual(
                (run_dir / "checks-2.json").read_text(encoding="utf-8"),
                original_checks,
            )
            self.assertNotEqual(
                (run_dir / "review-2.json").read_text(encoding="utf-8"),
                original_review,
            )
            self.assertTrue((run_dir / "repair-report-1.json").is_file())

    def test_legacy_flat_artifact_runs_remain_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_built_run(temp_path, environment)
            run_dir = data_dir / "runs" / run_id
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            build_ready = next(e for e in events if e["type"] == "build_ready")
            candidate_sha = build_ready["candidate_sha"]
            legacy_report = run_dir / "build-report.json"
            legacy_report.write_text(
                (run_dir / "build-report-1.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            rewritten = []
            for event in events:
                if event["type"] == "build_ready":
                    event = {**event, "artifact": str(legacy_report)}
                rewritten.append(event)
            (run_dir / "events.jsonl").write_text(
                "".join(json.dumps(event, sort_keys=True) + "\n" for event in rewritten),
                encoding="utf-8",
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
            self.assertEqual(json.loads(status.stdout)["state"], "built")
            self.assertEqual(json.loads(status.stdout)["candidate_sha"], candidate_sha)
            listed = agentflow(
                "list",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            entry = next(
                item for item in json.loads(listed.stdout) if item["run_id"] == run_id
            )
            self.assertEqual(entry["candidate_sha"], candidate_sha)

    def test_planner_receives_complete_frozen_task_object(self) -> None:
        from agentflow.workflow import advance_run

        class CapturingAdapter:
            name = "fake"

            def __init__(self) -> None:
                self.requests: list[dict] = []

            def invoke(self, *, role, request, workspace, transcript_path=None):
                self.requests.append(request)
                return {
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

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            source = {
                "provider": "github",
                "work_item_id": "11",
                "captured_at": "2026-07-15T12:00:00+00:00",
                "content_hash": "e" * 64,
            }
            task = {
                "acceptance_criteria": ["checks pass"],
                "source": source,
                "summary": "Add a health endpoint",
            }
            (data_dir / "runs" / run_id / "task.json").write_text(
                json.dumps(task, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            adapter = CapturingAdapter()
            planned = advance_run(
                run_id=run_id,
                data_dir=data_dir,
                adapter=adapter,
            )
            self.assertEqual(planned.state, "planned")
            self.assertEqual(adapter.requests[0]["task"], task)

    def test_checks_record_enriched_evidence_with_injected_seams(self) -> None:
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        from agentflow.workflow import (
            CHECK_ENV_ALLOWLIST,
            _run_profile_checks,
            advance_run,
            default_check_environment_fingerprint,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_built_run(temp_path, environment)
            run_dir = data_dir / "runs" / run_id

            ticks = iter(
                [datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)]
            )
            monos = iter([100.0, 100.005])
            secret_env = {
                "LANG": "should-be-overwritten",
                "PYTHONHASHSEED": "should-be-overwritten",
                "TZ": "should-be-overwritten",
                "AWS_SECRET_ACCESS_KEY": "super-secret",
                "TOKEN": "nope",
            }

            result = advance_run(
                run_id=run_id,
                data_dir=data_dir,
                adapter=None,
                clock=lambda: next(ticks),
                monotonic=lambda: next(monos),
                environment_fingerprint=lambda: default_check_environment_fingerprint(
                    environ=secret_env
                ),
            )
            self.assertEqual(result.state, "verified")
            report = json.loads((run_dir / "checks-1.json").read_text(encoding="utf-8"))
            check = report["checks"][0]
            self.assertEqual(check["attempt"], 1)
            self.assertEqual(check["duration_ms"], 5)
            self.assertEqual(check["started_at"], "2026-07-15T12:00:00+00:00")
            self.assertEqual(check["environment"]["LANG"], "C.UTF-8")
            self.assertEqual(check["environment"]["PYTHONHASHSEED"], "0")
            self.assertEqual(check["environment"]["TZ"], "UTC")
            self.assertEqual(
                set(CHECK_ENV_ALLOWLIST)
                | {
                    "python_implementation",
                    "python_version",
                    "os_system",
                    "os_release",
                    "machine",
                },
                set(check["environment"]),
            )
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", check["environment"])
            self.assertNotIn("TOKEN", check["environment"])

            # Multi-check stage: shared attempt/environment, distinct times.
            completed = MagicMock(
                returncode=0, stdout="ok", stderr=""
            )
            calls = {"n": 0}
            clock_ticks = iter(
                [
                    datetime(2026, 7, 15, 13, 0, 0, tzinfo=timezone.utc),
                    datetime(2026, 7, 15, 13, 0, 2, tzinfo=timezone.utc),
                ]
            )
            mono_ticks = iter([10.0, 10.004, 20.0, 20.012])

            def run_command(*args, **kwargs):
                calls["n"] += 1
                return completed

            fingerprint = {
                "LANG": "C.UTF-8",
                "PYTHONHASHSEED": "0",
                "TZ": "UTC",
                "python_implementation": "CPython",
                "python_version": "3.12.0",
                "os_system": "Darwin",
                "os_release": "25.0",
                "machine": "arm64",
            }
            checks, passed = _run_profile_checks(
                commands=[["echo", "a"], ["echo", "b"]],
                workspace=temp_path,
                attempt=3,
                environment={"LANG": "C.UTF-8"},
                environment_fingerprint=fingerprint,
                clock=lambda: next(clock_ticks),
                monotonic=lambda: next(mono_ticks),
                run_command=run_command,
            )
            self.assertTrue(passed)
            self.assertEqual(calls["n"], 2)
            self.assertEqual(checks[0]["attempt"], 3)
            self.assertEqual(checks[1]["attempt"], 3)
            self.assertEqual(checks[0]["environment"], fingerprint)
            self.assertEqual(checks[1]["environment"], fingerprint)
            self.assertEqual(checks[0]["started_at"], "2026-07-15T13:00:00+00:00")
            self.assertEqual(checks[1]["started_at"], "2026-07-15T13:00:02+00:00")
            self.assertEqual(checks[0]["duration_ms"], 4)
            self.assertEqual(checks[1]["duration_ms"], 12)

    def test_check_attempt_increments_across_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_tested_run(temp_path, environment)
            first_checks = json.loads(
                (data_dir / "runs" / run_id / "checks-1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(first_checks["checks"][0]["attempt"], 1)

            fixture_path = temp_path / "review-fixture.json"
            fixture_path.write_text(
                json.dumps(
                    {
                        "reviewer": {
                            "disposition": "changes_requested",
                            "findings": [
                                {
                                    "file": "README.md",
                                    "message": "Needs a tweak",
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

            repair_fixture = temp_path / "repair-fixture.json"
            repair_fixture.write_text(
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
                                "README.md": "# Target\n\nRepaired.\n",
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
                str(repair_fixture),
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            self.assertEqual(json.loads(repaired.stdout)["state"], "built")
            rechecked = agentflow(
                "advance",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(rechecked.returncode, 0, rechecked.stderr)
            second_checks = json.loads(
                (data_dir / "runs" / run_id / "checks-2.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(second_checks["checks"][0]["attempt"], 2)
            self.assertEqual(first_checks["checks"][0]["attempt"], 1)

    def test_candidate_generation_increments_after_rebase(self) -> None:
        from agentflow.workflow import _candidate_generation

        self.assertEqual(
            _candidate_generation(
                [
                    {"type": "build_ready"},
                    {"type": "checks_passed"},
                    {"type": "candidate_rebased"},
                ]
            ),
            2,
        )

    def test_tester_passing_test_commits_reruns_checks_and_reports_tested(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_verified_run(
                temp_path, environment, TEST_RUNNING_CHECK
            )
            run_dir = data_dir / "runs" / run_id
            verified_sha = next(
                event["candidate_sha"]
                for event in reversed(_events(data_dir, run_id))
                if event["type"] == "checks_passed"
            )

            tested = advance_tester(
                temp_path, data_dir, run_id, environment, TESTER_PASSING_FIXTURE
            )

            response = json.loads(tested.stdout)
            self.assertEqual(response["state"], "tested")
            self.assertNotEqual(response["candidate_sha"], verified_sha)
            post = json.loads(
                (run_dir / "checks-1-post-tests.json").read_text(encoding="utf-8")
            )
            self.assertEqual(post["candidate_sha"], response["candidate_sha"])
            self.assertTrue(post["workspace_clean"])
            self.assertTrue((run_dir / "checks-1.json").is_file())
            self.assertTrue((run_dir / "tester-report-1.json").is_file())
            tests_ready = next(
                event
                for event in reversed(_events(data_dir, run_id))
                if event["type"] == "tests_ready"
            )
            self.assertEqual(tests_ready["candidate_sha"], response["candidate_sha"])
            self.assertEqual(
                Path(tests_ready["checks_artifact"]).name, "checks-1-post-tests.json"
            )
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
            message = subprocess.run(
                ["git", "log", "-1", "--format=%s"],
                cwd=worktree,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(message, f"Agentflow run {run_id} tests 1")
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            replayed = json.loads(status.stdout)
            self.assertEqual(replayed["state"], "tested")
            self.assertEqual(replayed["candidate_sha"], response["candidate_sha"])

    def test_tester_failing_test_marks_run_failed_with_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_verified_run(
                temp_path, environment, TEST_RUNNING_CHECK
            )
            run_dir = data_dir / "runs" / run_id
            fixture_path = temp_path / "tester-fixture.json"
            fixture_path.write_text(json.dumps(TESTER_FAILING_FIXTURE), encoding="utf-8")

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
            self.assertEqual(json.loads(advanced.stdout)["state"], "failed")
            post = json.loads(
                (run_dir / "checks-1-post-tests.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                any(check["returncode"] != 0 for check in post["checks"])
            )
            tests_failed = next(
                event
                for event in reversed(_events(data_dir, run_id))
                if event["type"] == "tests_failed"
            )
            self.assertEqual(tests_failed["findings"][0]["severity"], "blocker")
            self.assertEqual(
                Path(tests_failed["checks_artifact"]).name, "checks-1-post-tests.json"
            )
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(json.loads(status.stdout)["state"], "failed")

    def test_tester_change_outside_test_paths_fails_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_verified_run(temp_path, environment)
            fixture_path = temp_path / "tester-fixture.json"
            fixture_path.write_text(
                json.dumps(TESTER_OUT_OF_PATH_FIXTURE), encoding="utf-8"
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
            self.assertIn("outside the declared test paths", advanced.stderr)
            self.assertIn("README.md", advanced.stderr)
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(json.loads(status.stdout)["state"], "verified")
            self.assertFalse(
                (data_dir / "runs" / run_id / "checks-1-post-tests.json").exists()
            )

    def test_tester_no_change_references_existing_checks_without_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_verified_run(temp_path, environment)
            run_dir = data_dir / "runs" / run_id
            verified_sha = next(
                event["candidate_sha"]
                for event in reversed(_events(data_dir, run_id))
                if event["type"] == "checks_passed"
            )

            tested = advance_tester(temp_path, data_dir, run_id, environment)

            response = json.loads(tested.stdout)
            self.assertEqual(response["state"], "tested")
            self.assertEqual(response["candidate_sha"], verified_sha)
            self.assertFalse((run_dir / "checks-1-post-tests.json").exists())
            tests_ready = next(
                event
                for event in reversed(_events(data_dir, run_id))
                if event["type"] == "tests_ready"
            )
            self.assertEqual(tests_ready["candidate_sha"], verified_sha)
            self.assertEqual(
                Path(tests_ready["checks_artifact"]).name, "checks-1.json"
            )

    def test_tester_requires_declared_test_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_verified_run(
                temp_path, environment, test_paths=None
            )
            fixture_path = temp_path / "tester-fixture.json"
            fixture_path.write_text(
                json.dumps(TESTER_NO_CHANGE_FIXTURE), encoding="utf-8"
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
            self.assertIn("regenerate the profile with --test-path", advanced.stderr)
            self.assertIn("start", advanced.stderr)
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(json.loads(status.stdout)["state"], "verified")

    def test_reviewer_receives_tester_findings_and_binds_to_tester_sha(self) -> None:
        from agentflow.workflow import advance_run

        class TesterThenReviewerAdapter:
            name = "fake"

            def __init__(self) -> None:
                self.reviewer_request: dict | None = None

            def invoke(self, *, role, request, workspace, transcript_path=None):
                if role == "tester":
                    tests_dir = workspace / "tests"
                    tests_dir.mkdir(parents=True, exist_ok=True)
                    (tests_dir / "test_probe.py").write_text(
                        "print('probe ok')\n", encoding="utf-8"
                    )
                    return {
                        "summary": "Probed the candidate with a new test.",
                        "files_changed": ["tests/test_probe.py"],
                        "findings": [
                            {
                                "file": None,
                                "message": "Consider covering the error path too",
                                "severity": "minor",
                            }
                        ],
                    }
                if role == "reviewer":
                    self.reviewer_request = request
                    return {"disposition": "approve", "findings": []}
                raise AssertionError(f"unexpected role {role}")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_verified_run(
                temp_path, environment, TEST_RUNNING_CHECK
            )
            verified_sha = next(
                event["candidate_sha"]
                for event in reversed(_events(data_dir, run_id))
                if event["type"] == "checks_passed"
            )
            adapter = TesterThenReviewerAdapter()

            tested = advance_run(run_id=run_id, data_dir=data_dir, adapter=adapter)
            self.assertEqual(tested.state, "tested")
            tester_sha = tested.candidate_sha
            self.assertNotEqual(tester_sha, verified_sha)

            reviewed = advance_run(run_id=run_id, data_dir=data_dir, adapter=adapter)
            self.assertEqual(reviewed.state, "awaiting_human")
            self.assertEqual(reviewed.candidate_sha, tester_sha)
            self.assertEqual(
                adapter.reviewer_request["tester_findings"],
                [
                    {
                        "file": None,
                        "message": "Consider covering the error path too",
                        "severity": "minor",
                    }
                ],
            )
            self.assertEqual(adapter.reviewer_request["candidate_sha"], tester_sha)

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
            self.assertEqual(json.loads(approved.stdout)["approved_sha"], tester_sha)

    def test_repair_after_block_reruns_tester_with_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_tested_run(temp_path, environment)
            run_dir = data_dir / "runs" / run_id
            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps(
                    {
                        "reviewer": {
                            "disposition": "changes_requested",
                            "findings": [
                                {
                                    "file": "README.md",
                                    "message": "Needs clearer docs",
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
                                "README.md": "# Target\n\nHealth endpoint documented well.\n"
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

            rechecked = agentflow(
                "advance",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(rechecked.returncode, 0, rechecked.stderr)
            self.assertEqual(json.loads(rechecked.stdout)["state"], "verified")

            tested = advance_tester(temp_path, data_dir, run_id, environment)
            self.assertEqual(json.loads(tested.stdout)["state"], "tested")
            # The chain re-ran at a new generation, preserving prior evidence.
            self.assertTrue((run_dir / "tester-report-1.json").is_file())
            self.assertTrue((run_dir / "tester-report-2.json").is_file())
            self.assertTrue((run_dir / "checks-2.json").is_file())

            fixture_path.write_text(
                json.dumps({"reviewer": {"disposition": "approve", "findings": []}}),
                encoding="utf-8",
            )
            rereviewed = agentflow(
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
            self.assertEqual(rereviewed.returncode, 0, rereviewed.stderr)
            self.assertEqual(json.loads(rereviewed.stdout)["state"], "awaiting_human")
            self.assertTrue((run_dir / "review-2.json").is_file())


if __name__ == "__main__":
    unittest.main()
