from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]


def run_agentflow(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


class StatusCommandTests(unittest.TestCase):
    def test_status_rebuilds_a_run_in_a_new_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "agentflow-home"
            repository.mkdir()
            git("init", cwd=repository)
            git("config", "user.email", "agentflow@example.test", cwd=repository)
            git("config", "user.name", "Agentflow Test", cwd=repository)
            (repository / "README.md").write_text("# Target\n", encoding="utf-8")
            git("add", "README.md", cwd=repository)
            git("commit", "-m", "Initial commit", cwd=repository)
            base_sha = git("rev-parse", "HEAD", cwd=repository)

            started = run_agentflow(
                "start",
                "Add a health endpoint",
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            run_id = json.loads(started.stdout)["run_id"]

            status = run_agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(
                json.loads(status.stdout),
                {
                    "base_sha": base_sha,
                    "repository": str(repository.resolve()),
                    "run_id": run_id,
                    "state": "ready",
                    "summary": "Add a health endpoint",
                    "worktree": str(data_dir.resolve() / "worktrees" / run_id),
                },
            )

    def test_status_includes_the_captured_repository_profile_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "agentflow-home"
            repository.mkdir()
            git("init", cwd=repository)
            git("config", "user.email", "agentflow@example.test", cwd=repository)
            git("config", "user.name", "Agentflow Test", cwd=repository)
            (repository / "README.md").write_text("# Target\n", encoding="utf-8")
            git("add", "README.md", cwd=repository)
            git("commit", "-m", "Initial commit", cwd=repository)

            profiled = run_agentflow(
                "profile",
                "--check",
                "python3 -m unittest discover -s tests -v",
                cwd=repository,
            )
            self.assertEqual(profiled.returncode, 0, profiled.stderr)
            git("add", "-f", ".agentflow/repository-profile.json", cwd=repository)
            git("commit", "-m", "Add repository profile", cwd=repository)

            started = run_agentflow(
                "start",
                "Add a health endpoint",
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            run_id = json.loads(started.stdout)["run_id"]

            status = run_agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(
                json.loads(status.stdout)["repository_profile_path"],
                ".agentflow/repository-profile.json",
            )

    def test_status_omits_empty_criteria_and_includes_source_when_present(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "agentflow-home"
            repository.mkdir()
            git("init", cwd=repository)
            git("config", "user.email", "agentflow@example.test", cwd=repository)
            git("config", "user.name", "Agentflow Test", cwd=repository)
            (repository / "README.md").write_text("# Target\n", encoding="utf-8")
            git("add", "README.md", cwd=repository)
            git("commit", "-m", "Initial commit", cwd=repository)
            base_sha = git("rev-parse", "HEAD", cwd=repository)

            legacy = run_agentflow(
                "start",
                "Legacy summary only",
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            legacy_id = json.loads(legacy.stdout)["run_id"]
            # Simulate a legacy on-disk task.json that predates criteria.
            (data_dir / "runs" / legacy_id / "task.json").write_text(
                json.dumps({"summary": "Legacy summary only"}, indent=2) + "\n",
                encoding="utf-8",
            )
            legacy_status = run_agentflow(
                "status",
                legacy_id,
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(legacy_status.returncode, 0, legacy_status.stderr)
            legacy_payload = json.loads(legacy_status.stdout)
            self.assertNotIn("acceptance_criteria", legacy_payload)
            self.assertNotIn("source", legacy_payload)
            self.assertEqual(
                legacy_payload,
                {
                    "base_sha": base_sha,
                    "repository": str(repository.resolve()),
                    "run_id": legacy_id,
                    "state": "ready",
                    "summary": "Legacy summary only",
                    "worktree": str(data_dir.resolve() / "worktrees" / legacy_id),
                },
            )

            with_criteria = run_agentflow(
                "start",
                "Criteria task",
                "--acceptance-criterion",
                "checks pass",
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(with_criteria.returncode, 0, with_criteria.stderr)
            criteria_id = json.loads(with_criteria.stdout)["run_id"]
            criteria_status = run_agentflow(
                "status",
                criteria_id,
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(criteria_status.returncode, 0, criteria_status.stderr)
            criteria_payload = json.loads(criteria_status.stdout)
            self.assertEqual(criteria_payload["acceptance_criteria"], ["checks pass"])
            self.assertNotIn("source", criteria_payload)

            source = {
                "provider": "github",
                "work_item_id": "7",
                "captured_at": "2026-07-15T12:00:00+00:00",
                "content_hash": "d" * 64,
            }
            task_path = temp_path / "imported.json"
            task_path.write_text(
                json.dumps(
                    {
                        "summary": "Imported task",
                        "acceptance_criteria": ["one"],
                        "source": source,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            imported = run_agentflow(
                "run",
                str(task_path),
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            imported_id = json.loads(imported.stdout)["run_id"]
            imported_status = run_agentflow(
                "status",
                imported_id,
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(imported_status.returncode, 0, imported_status.stderr)
            imported_payload = json.loads(imported_status.stdout)
            self.assertEqual(imported_payload["acceptance_criteria"], ["one"])
            self.assertEqual(imported_payload["source"], source)


if __name__ == "__main__":
    unittest.main()
